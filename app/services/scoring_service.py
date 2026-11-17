"""Step 3: judge model scores a company using ONLY verified, dated evidence."""

import json
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from app.providers.llm.llm_provider import LLMProvider
from app.schemas.job_spec_schema import JobSpec
from app.services.extraction_service import Extraction
from app.utils.dates import within_window


class ScoreOut(BaseModel):
    fit_score: int = Field(description="0-40: firmographic match to the ICP")
    intent_score: int = Field(description="0-40: strength and recency of buying signals")
    timing_score: int = Field(description="0-20: urgency/accessibility now")
    why_this_lead: str = Field(description="2-3 sentences citing the evidence ids, e.g. [E2]")
    outreach_angle: str
    risks: str
    cited_evidence_ids: list[str]


@dataclass
class Score:
    fit: int
    intent: int
    timing: int
    why: str
    angle: str
    risks: str
    cited: list[str]

    @property
    def total(self) -> int:
        return self.fit + self.intent + self.timing


def fresh_signals(extraction: Extraction, today: date, days: int):
    """Signals whose effective date (stated event date if earlier than publication) is inside the window."""
    return [
        s for s in extraction.signals if s.effective_date and within_window(s.effective_date, today, days)
    ]


def evidence_listing(extraction: Extraction, today: date, days: int) -> tuple[str, dict[str, str]]:
    """Numbered evidence for the judge; returns text and id -> source_url map."""
    lines, ids = [], {}
    for i, f in enumerate(extraction.facts, 1):
        ids[f"F{i}"] = f.source_url
        lines.append(f'[F{i}] {f.claim} | quote: "{f.quote}"')
    for i, s in enumerate(fresh_signals(extraction, today, days), 1):
        ids[f"E{i}"] = s.source_url
        extra = f", {len(s.corroborating_urls) + 1} sources" if s.corroborating_urls else ""
        label = f", intent: {s.intent}" if s.intent else ""
        lines.append(
            f"[E{i}] ({s.signal_type}, dated {s.effective_date.isoformat()}{extra}{label}, "
            f'source: {s.source_class or "?"}, date confidence: {s.date_confidence or "?"}) {s.claim} | quote: "{s.quote}"'
        )
    for i, s in enumerate(extraction.against, 1):  # true, but counts against buying from the client
        ids[f"X{i}"] = s.source_url
        lines.append(f'[X{i}] AGAINST ({s.intent}: {s.intent_reason}) {s.claim} | quote: "{s.quote}"')
    return "\n".join(lines), ids


def _clamp(value: int, high: int) -> int:
    """Never trust model arithmetic or ranges."""
    return max(0, min(high, value))


async def score(
    extraction: Extraction, spec: JobSpec, llm: LLMProvider, today: date, stack_summary: str = ""
) -> Score:
    listing, ids = evidence_listing(extraction, today, spec.freshness_days)
    playbook = [
        {"type": sig.type, "means": sig.description, "strength": sig.strength}
        for sig in spec.playbook.signals
    ]
    data = (
        json.dumps(
            {
                "icp": spec.icp,
                "ask": spec.ask,
                "signal_playbook": playbook,
                "negative_signals": spec.playbook.negative,
            }
        )
        + f"\n\nCompany: {extraction.company_name} — {extraction.description}\n\nVerified evidence:\n{listing}"
        + (f"\n\nCombined buying picture: {stack_summary}" if stack_summary else "")
    )
    out = await llm.parse(
        llm.judge_model,
        "You qualify B2B leads. Score only from the verified evidence listed; never assume facts that are not "
        "listed. If evidence is thin, score low. An event is not automatically a buying need: weigh each signal by "
        "its playbook strength and whether it plausibly creates a need for what the client sells (e.g. hiring "
        "in-house engineers may mean building internally, not buying from an agency). With no signals, intent is "
        "0-5. Each signal is labelled with its intent class; 'budget_only' alone is never high intent. [X] items "
        "are evidence AGAINST buying (building in-house, unrelated): weigh them down. Several independent buying "
        "signs of DIFFERENT kinds (e.g. contract hiring and a rebuild project) are much stronger than one. Cite evidence ids in "
        "why_this_lead. Today is "
        f"{today.isoformat()}.",
        data,
        ScoreOut,
        max_output_tokens=3000,
        effort="medium",
    )
    return Score(
        _clamp(out.fit_score, 40),
        _clamp(out.intent_score, 40),
        _clamp(out.timing_score, 20),
        out.why_this_lead,
        out.outreach_angle,
        out.risks,
        [c for c in out.cited_evidence_ids if c in ids],
    )
