"""Step 4: decision-makers (from verified page evidence + registry officers) and verified emails."""
import asyncio
import logging
import re
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import dns.resolver

from app.core.cost import CostCapExceeded
from app.core.normalize import email_matches_person
from app.models.enums import BuyerRole, EmailSource, EmailVerdict
from app.pipeline.extract import Extraction
from app.providers.email import EmailFinder, find_with_waterfall
from app.providers.registry.companies_house import Officer

# A "director" of art/design/creative/marketing content is not the budget holder for an ops purchase.
_NON_BUYER = re.compile(r"\b(art|creative|design|content|social|brand|marketing|non-executive|non executive)\s+director\b")
log = logging.getLogger("leadpro")

ECONOMIC_TITLES = ("founder", "owner", "ceo", "chief executive", "managing director", "md", "director", "partner", "president")


@dataclass
class DecisionMaker:
    first_name: str
    last_name: str
    job_title: str
    buyer_role: BuyerRole
    source_url: str
    source_quote: str
    email: str | None = None
    email_source: EmailSource | None = None
    email_verdict: EmailVerdict = EmailVerdict.NOT_CHECKED
    finder_status: str | None = None


_mx_cache: dict[str, bool] = {}


async def has_mx(domain: str) -> bool:
    if domain not in _mx_cache:
        try:
            answer = await dns.asyncresolver.resolve(domain, "MX", lifetime=10)
            _mx_cache[domain] = len(answer) > 0
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            _mx_cache[domain] = False  # definitively no mailbox
        except (dns.exception.DNSException, OSError):
            return True  # timeout/transient: don't conclude "no mailbox"; the finder bills only found emails
    return _mx_cache[domain]


def _has_term(title: str, term: str) -> bool:
    """Whole-word match: 'COO' must not match 'Coordinator', nor 'partner' match 'Partnerships'."""
    return re.search(rf"(?<![a-z]){re.escape(term.lower())}(?![a-z])", title) is not None


def _title_rank(title: str, buyer_titles: list[str]) -> tuple[int, BuyerRole] | None:
    t = title.lower()
    economic = any(_has_term(t, e) for e in ECONOMIC_TITLES) and not _NON_BUYER.search(t)
    for i, wanted in enumerate(buyer_titles):
        if _has_term(t, wanted):
            return i, BuyerRole.ECONOMIC_BUYER if economic else BuyerRole.FUNCTIONAL_OWNER
    if economic:
        return len(buyer_titles), BuyerRole.ECONOMIC_BUYER
    return None


def rank_decision_makers(extraction: Extraction, officers: list[Officer], officers_url: str | None,
                         buyer_titles: list[str], limit: int) -> list[DecisionMaker]:
    ranked: list[tuple[int, DecisionMaker]] = []
    seen: set[tuple[str, str]] = set()
    for p in extraction.people:
        r = _title_rank(p.job_title, buyer_titles)
        key = (p.first_name.lower(), p.last_name.lower())
        if r and key not in seen:
            seen.add(key)
            ranked.append((r[0], DecisionMaker(p.first_name, p.last_name, p.job_title, r[1], p.source_url, p.quote)))
    for o in officers:  # registry directors: authoritative, slightly lower priority than a matching title
        key = (o.first_name.lower(), o.last_name.lower())
        if key not in seen and officers_url:
            seen.add(key)
            title = o.role.replace("-", " ").title()
            ranked.append((len(buyer_titles) + 1, DecisionMaker(o.first_name, o.last_name, title, BuyerRole.ECONOMIC_BUYER,
                                                                officers_url, f"{o.last_name.upper()}, {o.first_name} — {title}")))
    ranked.sort(key=lambda x: x[0])
    return [dm for _, dm in ranked[:limit]]


async def attach_emails(people: list[DecisionMaker], site_emails: list[str], domain: str, finders: list[EmailFinder],
                        known: dict[tuple[str, str], str] | None = None,
                        suppressed_emails: frozenset[str] = frozenset()) -> None:
    """Already-verified emails first (free), then emails printed on the company's own pages (validated),
    then the finder waterfall. Only VALID (not catch-all, not unknown) emails are kept.

    An email in `suppressed_emails` (opt-out / erasure request) is never attached, even if it was
    verified before, is printed on the site, or a finder returns it again."""
    if not people or not finders:
        return
    for dm in people:
        if known and (email := known.get((dm.first_name.lower(), dm.last_name.lower()))) and email not in suppressed_emails:
            dm.email, dm.email_source, dm.email_verdict, dm.finder_status = email, EmailSource.FINDER, EmailVerdict.VALID, "reused"
            continue
        printed = [e for e in site_emails if email_matches_person(e, dm.first_name, dm.last_name) and e not in suppressed_emails]
        if printed:
            res = next((r for r in await finders[0].validate(printed[:1]) if r.email == printed[0]), None)
            if res and res.verdict == EmailVerdict.VALID:
                dm.email, dm.email_source, dm.email_verdict, dm.finder_status = res.email, EmailSource.COMPANY_PAGE, res.verdict, res.raw_status
                continue
    if not await has_mx(domain):  # no mailbox can exist; don't pay for lookups
        return
    pending = [dm for dm in people if dm.email is None]
    results = await asyncio.gather(*(find_with_waterfall(finders, dm.first_name, dm.last_name, domain) for dm in pending),
                                   return_exceptions=True)
    for dm, res in zip(pending, results):
        if isinstance(res, CostCapExceeded):
            raise res
        if isinstance(res, Exception):  # one slow/failed lookup must not lose the whole lead
            log.warning("email lookup failed for %s %s @ %s: %s", dm.first_name, dm.last_name, domain, type(res).__name__)
            dm.finder_status = f"error: {type(res).__name__}"
            continue
        if res is None:
            continue
        if res.email and res.email in suppressed_emails:
            dm.finder_status = "suppressed"
            continue
        dm.finder_status = res.raw_status
        dm.email_verdict = res.verdict
        if res.verdict == EmailVerdict.VALID and res.email:
            dm.email, dm.email_source = res.email, EmailSource.FINDER
