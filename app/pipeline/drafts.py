"""Optional step 5: one personalized cold email per decision-maker, grounded in verified evidence.

Truthful urgency only: the hook comes from a dated, verified signal. Code rejects a draft that
cites unknown evidence ids or contains numbers not present in the cited evidence.
"""
import re
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel

from app.config import get_settings
from app.pipeline.contacts import DecisionMaker
from app.pipeline.extract import Extraction
from app.pipeline.jobspec import JobSpec
from app.pipeline.score import Score, evidence_listing
from app.providers.llm.openai_provider import OpenAIProvider

OPT_OUT_LINE = "If this isn't relevant, just reply \"no\" and I won't contact you again."
_NUMBER = re.compile(r"\d[\d,.]*")


class DraftOut(BaseModel):
    subject: str
    body: str
    cited_evidence_ids: list[str]


@dataclass
class Draft:
    subject: str
    body: str
    evidence_ids: list[str]


def draft_is_grounded(draft: DraftOut, listing_by_id: dict[str, str]) -> bool:
    if not draft.cited_evidence_ids or any(i not in listing_by_id for i in draft.cited_evidence_ids):
        return False
    cited_text = " ".join(listing_by_id[i] for i in draft.cited_evidence_ids)
    return all(n.strip(".,") in cited_text for n in _NUMBER.findall(f"{draft.subject} {draft.body}"))


async def write_draft(dm: DecisionMaker, extraction: Extraction, score: Score, spec: JobSpec,
                      llm: OpenAIProvider, today: date) -> Draft | None:
    listing, _ = evidence_listing(extraction, today, spec.freshness_days)
    listing_by_id = {m.group(1): m.group(0) for m in re.finditer(r"\[([EF]\d+)\][^\n]*", listing)}
    data = (f"Sender's company: {spec.client.name} ({spec.client.website or ''}). Offer: {spec.ask}\n"
            f"Recipient: {dm.first_name} {dm.last_name}, {dm.job_title} at {extraction.company_name}\n"
            f"Angle: {score.angle}\n\nVerified evidence:\n{listing}")
    for _ in range(2):  # one retry if the first draft is not grounded
        out = await llm.parse(
            get_settings().llm_judge_model,
            "Write one short cold email (max 110 words, plain text, no links) from the sender to the recipient. "
            "Open with a specific hook from a dated signal in the evidence. Create urgency only from real facts "
            "in the evidence (timing of their hiring, expansion, launch) — never invent deadlines, scarcity, "
            "numbers or claims. End with a low-friction question. Cite the evidence ids you used.",
            data,
            DraftOut,
            effort="medium",
        )
        if draft_is_grounded(out, listing_by_id):
            return Draft(out.subject.strip(), f"{out.body.strip()}\n\n{OPT_OUT_LINE}", out.cited_evidence_ids)
    return None
