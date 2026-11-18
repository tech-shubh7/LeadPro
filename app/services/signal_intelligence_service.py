"""Is this buying sign a real buying opportunity for THIS client?

A verified quote proves the words are on the page; it does not prove the company wants to buy what the
client sells. "We're hiring a permanent full-stack engineer to grow our in-house team" is a real, dated,
quoted signal, and it is evidence the company will NOT hire an agency (jobs 17-21 were full of these).

Every verified signal gets three labels:
- source class and trust (rules: where the quote came from),
- date confidence (rules: how the date was established; a date taken only from the URL is low),
- intent (one batched model call per company, constrained to fixed classes, given the client's offer).

Only signals with buying intent count as buying signs. In-house builds and unrelated signals become
evidence against the company, shown to the scorer, never counted for it.
"""

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.models.enums import DateSource
from app.schemas.job_spec_schema import JobSpec
from app.utils.logger import get_logger
from app.utils.normalize import normalize_domain

logger = get_logger(__name__)

Intent = Literal[
    "asks_for_supplier",  # RFP, brief, "looking for an agency / partner / quotes"
    "outsourcing_signal",  # contract / freelance / interim / overflow help, a partner search
    "project_needs_offer",  # a committed project (launch, rebuild, migration) that needs what the client sells
    "budget_only",  # funding, growth, new site: money exists, nothing says they want this
    "in_house_build",  # hiring permanent staff to do exactly this themselves: against buying outside
    "unrelated",  # true, but nothing to do with what the client sells
]
# Never chosen by the model: set in code for clients that sell capacity (white-label, outsourced development)
# when a company's own job posting hires for the work the client supplies. For those clients a hire is the
# need itself ("you're hiring a WordPress developer; our team starts this week"), not a reason they won't buy.
HIRING_FOR_OFFER = "hiring_for_offer"
BUYING = frozenset(
    {"asks_for_supplier", "outsourcing_signal", "project_needs_offer", HIRING_FOR_OFFER, "budget_only"}
)
AGAINST = frozenset({"in_house_build", "unrelated"})

INTENT_LABELS = {
    "asks_for_supplier": "Asked for a supplier",
    "outsourcing_signal": "Looking for outside help",
    "project_needs_offer": "Project needs this work",
    "budget_only": "Has budget only",
    HIRING_FOR_OFFER: "Hiring for this skill (not a partner request)",
    "in_house_build": "Building it in-house",
    "unrelated": "Not related",
}

# Source classes, most to least trustworthy. A starting policy, not a truth score: the label is shown
# with every signal so a person can judge, and trust only decides tie-breaks, never qualification alone.
SOURCE_TRUST = {
    "official_notice": "high",
    "job_posting": "high",
    "company_site": "high",
    "company_document": "high",
    "news": "medium",
    "document": "medium",
    "other": "low",
}
_DATE_CONFIDENCE = {
    DateSource.OFFICIAL_NOTICE: "high",
    DateSource.JOB_POSTING: "high",
    DateSource.ARTICLE_META: "high",
    DateSource.JSON_LD: "high",
    DateSource.DOCUMENT_META: "medium",
    DateSource.HTML_MARKUP: "medium",
    DateSource.SEARCH_RESULT: "medium",
    DateSource.URL_PATH: "low",
}
_CAREERS_PATH = re.compile(r"/(careers?|jobs?|vacancies|join-us|work-with-us)(/|$)", re.I)
_OFFICIAL_HOSTS = ("contractsfinder.service.gov.uk", "find-tender.service.gov.uk", "sam.gov")


def source_class(url: str, company_domain: str, seed_kinds: dict[str, str]) -> str:
    kind = seed_kinds.get(url, "")
    hostname = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if kind == "notice" or any(hostname == h or hostname.endswith(f".{h}") for h in _OFFICIAL_HOSTS):
        return "official_notice"
    if kind in ("ats", "job_ad"):
        return "job_posting"
    own = normalize_domain(url) == company_domain
    if url.lower().split("?")[0].endswith(".pdf"):
        return "company_document" if own else "document"
    if own:
        return "job_posting" if _CAREERS_PATH.search(url) else "company_site"
    return "news" if kind == "news" else "other"


def date_confidence(signal) -> str:
    if signal.trusted_date is None:
        return "none"
    if signal.corroborating_urls and signal.trusted_date.source == DateSource.URL_PATH:
        return "medium"  # an URL date backed by another source reporting the same event
    return _DATE_CONFIDENCE.get(signal.trusted_date.source, "medium")


class SignalIntent(BaseModel):
    signal_index: int
    intent: Intent
    reason: str = Field(description="One short sentence, citing what in the quote decides it")


class IntentOut(BaseModel):
    items: list[SignalIntent]


INTENT_INSTRUCTIONS = (
    "For each signal about this company, decide whether it shows the company is likely to BUY the "
    "client's offer from an OUTSIDE supplier. Classes:\n"
    "- asks_for_supplier: it asks for proposals, quotes, an agency or a partner for this kind of work.\n"
    "- outsourcing_signal: it wants outside help: contract, freelance, interim or overflow capacity.\n"
    "- project_needs_offer: a committed project (launch, rebuild, migration) that needs the client's offer.\n"
    "- budget_only: funding or growth: money exists, but nothing says they want this work.\n"
    "- in_house_build: it is hiring PERMANENT staff to do exactly this work itself, or says it builds in-house. "
    "This is evidence AGAINST buying from the client. But if the client sells capacity TO companies like this "
    "one (e.g. white-label development for agencies), the same hiring shows a need and is outsourcing_signal.\n"
    "- unrelated: true, but it has nothing to do with what the client sells.\n"
    "Judge from the quote, not from what the company might do. When unsure between two classes, choose "
    "the weaker one."
)


@dataclass
class IntentAssessment:
    buying: list = field(default_factory=list)  # signals that count as buying signs
    against: list = field(default_factory=list)  # signals that count against (in-house, unrelated)


async def assess_signals(
    signals: list, spec: JobSpec, company_name: str, company_domain: str, seed_kinds: dict[str, str], llm
) -> IntentAssessment:
    """Labels every signal (in place) and splits them into buying signs and evidence against.
    A model failure leaves intent unknown and keeps the signal as before: never worse than no check."""
    for s in signals:
        s.source_class = source_class(s.source_url, company_domain, seed_kinds)
        s.date_confidence = date_confidence(s)
    if not signals:
        return IntentAssessment()
    offer = spec.offer or (spec.sender.offer if spec.sender and spec.sender.offer else "") or spec.ask
    listing = "\n".join(
        f'[{i}] {s.signal_type}: {s.claim} | quote: "{s.quote}"' for i, s in enumerate(signals)
    )
    try:
        out = await llm.parse(
            llm.extract_model,
            INTENT_INSTRUCTIONS,
            f"Client's offer: {offer}\nWho the client wants: {spec.ask}\nCompany: {company_name}\n\nSignals:\n{listing}",
            IntentOut,
            max_output_tokens=2000,
        )
        verdicts = {v.signal_index: v for v in out.items if 0 <= v.signal_index < len(signals)}
    except Exception as exc:  # the scorer still sees every signal: the check is skipped, not failed
        logger.warning("intent check failed for %s: %s: %s", company_domain, type(exc).__name__, exc)
        verdicts = {}
    result = IntentAssessment()
    for i, s in enumerate(signals):
        if verdict := verdicts.get(i):
            s.intent, s.intent_reason = verdict.intent, verdict.reason
        _hiring_as_demand(s, spec)
        (result.against if s.intent in AGAINST else result.buying).append(s)
    return result


def _hiring_as_demand(signal, spec: JobSpec) -> None:
    """For a client that sells capacity, the company's own job posting for the work it supplies is demand.
    Deterministic, not left to the model: only a posting (ATS, job board, careers page) the model itself
    judged to be hiring for exactly this work (in_house_build) is relabelled, never an unrelated one."""
    if spec.hiring_is_demand and signal.intent == "in_house_build" and signal.source_class == "job_posting":
        signal.intent = HIRING_FOR_OFFER
        signal.intent_reason = (
            f"hiring for work the client supplies as a white-label/outside team ({signal.intent_reason})"
        )


def can_qualify(signal) -> bool:
    """A buying sign that may qualify a company on its own: buying intent (or unclassified) and a date we
    can stand behind. A URL-only date can support, never qualify alone."""
    return signal.intent not in AGAINST and signal.date_confidence not in ("low", "none")


# --- combining a company's buying signs ----------------------------------------------------------
# One sign can mislead; several independent signs of different kinds rarely do. A company that asked for
# a supplier, or shows two different kinds of buying sign (contract hiring AND a rebuild project), is a far
# stronger lead than one with a single sign, and budget alone (funding, growth) says nothing about wanting
# this work. Deterministic: the tier never depends on a model.

TIER_STRONG, TIER_GOOD, TIER_WEAK = "strong", "good", "weak"
TIER_ORDER = {TIER_STRONG: 0, TIER_GOOD: 1, TIER_WEAK: 2, "": 3}
TIER_LABELS = {TIER_STRONG: "Strong", TIER_GOOD: "Good", TIER_WEAK: "Weak"}
# A company whose only signs are budget can never score high on intent, whatever the scorer says.
WEAK_INTENT_CAP = 15  # of 40
_DEMAND = ("asks_for_supplier", "outsourcing_signal", "project_needs_offer", HIRING_FOR_OFFER)
_STRENGTH_ORDER = {kind: i for i, kind in enumerate((*_DEMAND, "budget_only"))}


@dataclass(frozen=True)
class SignalStack:
    tier: str  # strong | good | weak | "" (no qualifying sign)
    kinds: tuple[str, ...]  # distinct intent classes, strongest first
    events: int  # distinct events (reports of one event are already merged)
    sources: int  # independent websites the signs come from
    summary: (
        str  # one line for people: "Asked for a supplier + Looking for outside help · 3 signs, 2 sources"
    )


def combine(signals: list) -> SignalStack:
    """The combined picture of a company's qualifying buying signs. An unclassified sign (intent check
    unavailable) counts like a single real sign: never better, never worse, than before the check existed."""
    if not signals:
        return SignalStack("", (), 0, 0, "")
    ranked = sorted({s.intent for s in signals if s.intent}, key=lambda k: _STRENGTH_ORDER.get(k, 9))
    demand = [k for k in ranked if k in _DEMAND]
    unclassified = any(not s.intent for s in signals)
    domains = {normalize_domain(u) for s in signals for u in (s.source_url, *s.corroborating_urls)} - {
        None,
        "",
    }
    # A hire is a need but not a request: it can make a lead good, and never makes one strong on its own or
    # by pairing with another sign (a freelance ad and a permanent ad are one need, not two kinds).
    if "asks_for_supplier" in demand or len(set(demand) - {HIRING_FOR_OFFER}) >= 2:
        tier = TIER_STRONG
    elif demand or unclassified:
        tier = TIER_GOOD
    else:
        tier = TIER_WEAK  # budget only
    kinds_text = " + ".join(INTENT_LABELS[k] for k in ranked) or "Buying sign"
    plural = "s" if len(signals) != 1 else ""
    summary = (
        f"{kinds_text} · {len(signals)} sign{plural}, {len(domains)} source{'s' if len(domains) != 1 else ''}"
    )
    return SignalStack(tier, tuple(ranked), len(signals), len(domains), summary)
