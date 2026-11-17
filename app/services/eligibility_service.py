"""Which channels a client can actually sell through, enforced in code.

A plan is only as good as its channels. Searching UK public tenders for an agency based in India is not
a weak plan, it is a wrong one: the client cannot bid (that needs a local entity, supplier registration
and usually a public-sector track record), so every lead it produces is unusable. The planning prompt
explains this, but a prompt can be ignored; this module cannot. It runs on every plan (drafted, fallback
and operator-edited) and removes what the client is not eligible for, recording why so the operator sees
exactly what was left out.
"""

import re
from dataclasses import dataclass, field

import phonenumbers

from app.schemas.client_profile_schema import ClientFactsOut, ClientProfile
from app.services.source_service import browse_url_problem
from app.utils.evidence_verifier import quote_in_text
from app.utils.normalize import extract_phones, normalize_domain

# Country-code top-level domains that reliably mean "based there". Generic TLDs (.com, .io) say nothing.
_CC_TLDS = {
    "in": "IN", "uk": "GB", "au": "AU", "ca": "CA", "de": "DE", "fr": "FR", "nl": "NL", "ie": "IE",
    "nz": "NZ", "sg": "SG", "ae": "AE", "za": "ZA", "es": "ES", "it": "IT", "pk": "PK", "bd": "BD",
    "lk": "LK", "ph": "PH", "vn": "VN", "ua": "UA", "pl": "PL", "ro": "RO", "br": "BR", "mx": "MX",
}  # fmt: skip

# Words that mark a search as hunting for public procurement: open tenders, contract notices, frameworks.
# Private companies say "RFP" and "request for proposal" too, so those alone are not procurement.
PROCUREMENT = re.compile(
    r"\b(?:tenders?|tendering|invitation to tender|itt|procurement|contract notices?|find[- ]a[- ]tender|"
    r"find-tender|contracts ?finder|contractsfinder|framework agreement|dynamic purchasing|g-cloud|"
    r"digital marketplace|public sector|council|nhs|gov\.uk|\.gov\b|government)\b",
    re.I,
)


@dataclass
class Exclusion:
    """Something left out of the plan, and the reason in plain words for the operator."""

    what: str
    why: str


@dataclass
class EligibilityResult:
    signals: list[dict]
    sources: list[dict] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)


def verify_client_facts(facts: ClientFactsOut, site_text: str, website: str | None) -> ClientProfile:
    """Keeps only facts whose quoted words are really on the client's site. Where the model's location is
    unproven, falls back to deterministic hints: a country-code domain, then the country of the phone
    numbers printed on the site (only when they all agree)."""
    profile = ClientProfile()
    country = facts.based_in_country.strip().upper()
    if country and facts.based_in_quote and quote_in_text(facts.based_in_quote, site_text):
        profile.country, profile.country_evidence = country, f'site says "{facts.based_in_quote.strip()}"'
    elif hint := _country_hint(website, site_text):
        profile.country, profile.country_evidence = hint
    if facts.public_sector_quote and quote_in_text(facts.public_sector_quote, site_text):
        profile.public_sector_evidence = facts.public_sector_quote.strip()
    return profile


def _country_hint(website: str | None, site_text: str) -> tuple[str, str] | None:
    domain = normalize_domain(website or "")
    if domain and (code := _CC_TLDS.get(domain.rsplit(".", 1)[-1])):
        return code, f"its website is on a .{domain.rsplit('.', 1)[-1]} domain"
    regions = {
        phonenumbers.region_code_for_number(phonenumbers.parse(hit.e164))
        for hit in extract_phones(site_text, "ZZ")  # ZZ: only numbers written with a +country code
    }
    regions.discard(None)
    if len(regions) == 1:
        region = regions.pop()
        return region, f"every phone number on its site is a {region} number"
    return None


def can_bid_public_tenders(profile: ClientProfile, target_country: str) -> tuple[bool, str]:
    """Public contracts need a local entity and, in practice, a public-sector track record."""
    if profile.country and profile.country != target_country.upper():
        return False, (
            f"the client is based in {profile.country} ({profile.country_evidence}), and public "
            f"tenders in {target_country.upper()} need a local entity and supplier registration"
        )
    if not profile.has_public_sector_record:
        return (
            False,
            "the client's site shows no public-sector work, which public tenders effectively require",
        )
    return True, ""


def apply_eligibility(
    signals: list[dict], profile: ClientProfile, target_country: str, sources: list[dict] | None = None
) -> EligibilityResult:
    """Removes the searches and sources the client cannot act on. A signal left with no query is dropped."""
    allowed, why = can_bid_public_tenders(profile, target_country)
    exclusions: list[Exclusion] = []
    kept_sources = []
    for source in sources or []:
        if problem := _source_problem(source, allowed, why):
            exclusions.append(Exclusion(f"Source: {_source_label(source)}", problem))
        else:
            kept_sources.append(dict(source))
    if allowed:
        return EligibilityResult(
            signals=[dict(s) for s in signals], sources=kept_sources, exclusions=exclusions
        )
    kept: list[dict] = []
    for signal in signals:
        queries = [q for q in signal.get("queries", []) if not PROCUREMENT.search(q)]
        removed = len(signal.get("queries", [])) - len(queries)
        procurement_signal = bool(
            PROCUREMENT.search(f"{signal.get('type', '')} {signal.get('description', '')}")
        )
        label = signal.get("type", "").replace("_", " ")
        if procurement_signal or not queries:
            exclusions.append(Exclusion(f"Signal: {label}", f"Public tenders are skipped: {why}."))
            continue
        if removed:
            exclusions.append(
                Exclusion(f"{removed} tender search(es) in {label}", f"Public tenders are skipped: {why}.")
            )
        kept.append({**signal, "queries": queries})
    return EligibilityResult(signals=kept, sources=kept_sources, exclusions=exclusions)


def _source_label(source: dict) -> str:
    return source.get("url") or source.get("category") or source.get("type", "source").replace("_", " ")


def _source_problem(source: dict, tenders_allowed: bool, tenders_why: str) -> str | None:
    """Why a structured source is left out, or None to keep it. Award notices list contract WINNERS (to
    sell to), not tenders to bid on, so they never need tender eligibility."""
    if source.get("type") != "agent_browse":
        return None
    if problem := browse_url_problem(source.get("url")):
        return f"Not browsed: {problem}."
    if not tenders_allowed and PROCUREMENT.search(f"{source.get('url', '')} {source.get('goal', '')}"):
        return f"Public tenders are skipped: {tenders_why}."
    return None
