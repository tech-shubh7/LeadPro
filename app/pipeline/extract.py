"""Step 2b: LLM extraction (cheap model) + code-level quote verification + trusted dating."""
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.dates import TrustedDate, extract_page_date, parse_event_date, parse_relative_date
from app.core.evidence_verify import Claim, canonical_url, normalize_text, phrase_near_quote, verify_claims
from app.models.enums import DateSource
from app.pipeline.jobspec import JobSpec
from app.pipeline.research import ResearchBundle
from app.providers.llm.openai_provider import OpenAIProvider



class QuotedFact(BaseModel):
    claim: str
    source_url: str
    quote: str = Field(description="Exact text copied from the source, at least 6 words")


class QuotedSignal(QuotedFact):
    signal_type: str = Field(description="One of the playbook signal types, or 'other'")
    event_date_quote: str = Field("", description=(
        "If the source states WHEN the event itself happened (e.g. 'in March 2025', 'on 3 June 2026'), copy that "
        "exact date phrase verbatim from the same source, close to the quote. Empty if no date is stated."))
    event_key: str = Field("", description=(
        "A short label for the real-world event (e.g. 'series-a-funding', 'leeds-warehouse-opening'). Use the SAME "
        "label for every source that reports the same event (republished or syndicated news); different events get "
        "different labels."))


class QuotedPerson(BaseModel):
    first_name: str
    last_name: str
    job_title: str
    source_url: str
    quote: str = Field(description="Exact text from the source that shows this person's name and role")


class ExtractOut(BaseModel):
    sources_match_target: bool = Field(description="False if the website/sources are about a different organisation than the target")
    company_name: str
    company_description: str
    is_recruitment_or_staffing_firm: bool
    facts: list[QuotedFact] = Field(description="Firmographics: industry, location, size, products, customers")
    signals: list[QuotedSignal]
    people: list[QuotedPerson]


@dataclass
class VerifiedSignal:
    signal_type: str
    claim: str
    source_url: str
    quote: str
    trusted_date: TrustedDate | None  # when the source was published (trusted sources only)
    event_date: date | None = None  # when the event happened, if the source states it (verified phrase)
    event_date_quote: str = ""
    event_key: str = ""
    corroborating_urls: list[str] = field(default_factory=list)  # other sources reporting the same event

    @property
    def effective_date(self) -> date | None:
        """The date freshness is judged on: the stated event date when it is earlier than publication
        (an article published today about an event 8 months ago is an 8-month-old signal). A stated date
        can only make a signal older, never fresher, and never dates a signal whose source is undated."""
        if self.trusted_date is None:
            return None
        if self.event_date and self.event_date < self.trusted_date.value:
            return self.event_date
        return self.trusted_date.value

    def date_basis(self) -> str:
        if self.trusted_date is None:
            return ""
        basis = f"published {self.trusted_date.value.isoformat()} ({self.trusted_date.source.value})"
        if self.effective_date != self.trusted_date.value:
            basis = f"event stated as '{self.event_date_quote}'; source {basis}"
        if self.corroborating_urls:
            basis += f"; same event also reported by {len(self.corroborating_urls)} other source(s)"
        return basis


@dataclass
class Extraction:
    company_name: str
    description: str
    is_staffing: bool
    facts: list[Claim] = field(default_factory=list)
    signals: list[VerifiedSignal] = field(default_factory=list)
    people: list[QuotedPerson] = field(default_factory=list)
    rejected_count: int = 0
    sources_match_target: bool = True


_ABS_DATE_FORMATS = ("%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d %B %Y", "%Y-%m-%d")


def parse_search_date(text: str | None, today: date) -> TrustedDate | None:
    if not text:
        return None
    for fmt in _ABS_DATE_FORMATS:
        try:
            return TrustedDate(datetime.strptime(text.strip(), fmt).date(), DateSource.SEARCH_RESULT)
        except ValueError:
            continue
    return parse_relative_date(text, today, DateSource.SEARCH_RESULT)


def build_sources(bundle: ResearchBundle) -> tuple[str, dict[str, str]]:
    """LLM input and the texts claims are verified against (keyed by URL)."""
    texts: dict[str, str] = {url: page.text for url, page in bundle.pages.items() if page.text}
    for hit in bundle.seed_hits:
        texts.setdefault(hit.link, f"{hit.title}\n{hit.snippet}")
    limit = get_settings().llm_max_page_chars  # limits what the model reads; verification uses full text
    blocks = [f'<source url="{url}">\n{text[:limit]}\n</source>' for url, text in texts.items()]
    return "\n\n".join(blocks), texts


def _signal_date(url: str, bundle: ResearchBundle, today: date) -> TrustedDate | None:
    page = bundle.pages.get(url)
    if page is not None and (d := extract_page_date(page.html, url)):
        return d
    for hit in bundle.seed_hits:
        if hit.link == url:
            searched_on = date.fromisoformat(hit.searched_on) if hit.searched_on else today
            return parse_search_date(hit.date_text, searched_on)
    return None


def _event_date(sig: QuotedSignal, texts: dict[str, str]) -> date | None:
    """Stated event date, accepted only if its phrase is verbatim in the same source near the signal quote,
    and parsed by code (the model's reading of the date is never trusted)."""
    phrase = sig.event_date_quote.strip()
    if not phrase:
        return None
    text = texts.get(sig.source_url)
    if text is None:
        text = next((t for u, t in texts.items() if canonical_url(u) == canonical_url(sig.source_url)), None)
    if text is None or not phrase_near_quote(phrase, sig.quote, text):
        return None
    return parse_event_date(phrase)


def collapse_duplicate_events(signals: list[VerifiedSignal]) -> list[VerifiedSignal]:
    """One signal per real-world event. Republished/syndicated reports of the same event are merged; the
    earliest-dated report is kept (it is closest to when the event actually happened) and the others are
    listed as corroborating sources. Merging can only reduce counted signals, so it fails safe."""
    groups: dict[tuple[str, str], list[VerifiedSignal]] = {}
    for sig in signals:
        key = normalize_text(sig.event_key) or "claim:" + normalize_text(sig.claim)
        groups.setdefault((sig.signal_type.strip().lower(), key), []).append(sig)
    merged = []
    for members in groups.values():
        members.sort(key=lambda m: (m.effective_date is None, m.effective_date or date.max))
        head = members[0]
        head.corroborating_urls = list(dict.fromkeys(m.source_url for m in members[1:] if m.source_url != head.source_url))
        merged.append(head)
    return merged


async def extract(bundle: ResearchBundle, spec: JobSpec, llm: OpenAIProvider, today: date) -> Extraction:
    data, texts = build_sources(bundle)
    signal_types = ", ".join(s.type for s in spec.playbook.signals)
    target = f"Target: {bundle.target_name or bundle.domain}" + (f" ({bundle.target_category})" if bundle.target_category else "")
    out = await llm.parse(
        get_settings().llm_extract_model,
        f"{target}. First decide whether the sources are about this target (set sources_match_target=false if the "
        "website belongs to an unrelated organisation with a similar name). Extract facts about the company whose website is "
        f"{bundle.domain}. Every item must cite the source url it came from and quote the exact text "
        "(copied verbatim, at least 6 words). Do not use outside knowledge. Signals are recent events that "
        f"suggest buying intent; classify them as one of: {signal_types}, or 'other'. For each signal, copy the "
        "stated date of the event itself if the source gives one (an article can be new while the event is old), "
        "and give the same event_key to every source describing the same event. "
        "People: only named individuals with a stated role at this company.",
        data,
        ExtractOut,
    )
    facts_v, rej1 = verify_claims([Claim(f.claim, f.source_url, f.quote) for f in out.facts], texts)
    signals_v, rej2 = verify_claims([Claim(s.claim, s.source_url, s.quote) for s in out.signals], texts)
    people_v, rej3 = verify_claims([Claim(f"{p.first_name} {p.last_name}", p.source_url, p.quote) for p in out.people], texts)

    by_key = {(s.claim, s.source_url, s.quote): s for s in out.signals}
    signals = []
    for c in signals_v:
        raw = by_key[(c.claim, c.source_url, c.quote)]
        event_date = _event_date(raw, texts)
        signals.append(VerifiedSignal(raw.signal_type, c.claim, c.source_url, c.quote, _signal_date(c.source_url, bundle, today),
                                      event_date=event_date, event_date_quote=raw.event_date_quote.strip() if event_date else "",
                                      event_key=raw.event_key.strip()))
    signals = collapse_duplicate_events(signals)
    ok_people = {(c.source_url, c.quote) for c in people_v}
    # The quote must actually contain the person's surname, not just any text from the page.
    people = [
        p for p in out.people
        if (p.source_url, p.quote) in ok_people and re.search(rf"\b{re.escape(p.last_name)}\b", p.quote, re.I)
    ]
    return Extraction(
        company_name=out.company_name,
        description=out.company_description,
        is_staffing=out.is_recruitment_or_staffing_firm,
        facts=facts_v,
        signals=signals,
        people=people,
        rejected_count=len(rej1) + len(rej2) + len(rej3) + (len(people_v) - len(people)),
        sources_match_target=out.sources_match_target,
    )
