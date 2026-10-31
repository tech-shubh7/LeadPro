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


MAX_BODY_WORDS = 110

COLD_EMAIL_RULES = (
    "Write one cold email from the sender to the recipient, following proven cold-email practice:\n"
    "- Subject: 2-5 plain words, specific to them, no clickbait, no exclamation marks.\n"
    "- Line 1: their specific, dated trigger from the evidence (what they did), in plain words. No 'I hope this "
    "finds you well', no 'I came across', no flattery.\n"
    "- Line 2: the likely consequence of that trigger for them (the work it creates), as a reasonable "
    "inference, not a claim about their internals.\n"
    "- Line 3: one concrete thing the sender could do for them, tied to that trigger (not a services list).\n"
    "- Why the sender: at most one sentence, using ONLY the sender's 'why choose' points given (never invent "
    "experience, clients, results or numbers). Omit it if none are given.\n"
    "- End with one low-friction yes/no question (interest, not a meeting time).\n"
    f"- 50-{MAX_BODY_WORDS - 10} words total, plain text, short paragraphs, no links, no attachments, no buzzwords "
    "(leverage, synergy, cutting-edge, revolutionize), no fake urgency or scarcity.\n"
    "- Write as the sender, first person, peer to peer. Do not add a signature or opt-out (added by code).\n"
    "Cite the evidence ids you used."
)


def draft_is_grounded(draft: DraftOut, listing_by_id: dict[str, str], sender_text: str = "") -> bool:
    """Every cited id exists, the body is short, and every number comes from cited evidence or the sender's
    own stated points (no invented figures)."""
    if not draft.cited_evidence_ids or any(i not in listing_by_id for i in draft.cited_evidence_ids):
        return False
    if len(draft.body.split()) > MAX_BODY_WORDS:
        return False
    allowed = " ".join(listing_by_id[i] for i in draft.cited_evidence_ids) + " " + sender_text
    return all(n.strip(".,") in allowed for n in _NUMBER.findall(f"{draft.subject} {draft.body}"))


async def write_draft(dm: DecisionMaker, extraction: Extraction, score: Score, spec: JobSpec,
                      llm: OpenAIProvider, today: date) -> Draft | None:
    listing, _ = evidence_listing(extraction, today, spec.freshness_days)
    listing_by_id = {m.group(1): m.group(0) for m in re.finditer(r"\[([EF]\d+)\][^\n]*", listing)}
    sender = spec.sender
    if sender:
        sender_text = f"{sender.name}. Offer: {sender.offer}. Why choose: " + (" | ".join(sender.why_choose) or "(none)")
    else:
        sender_text = f"{spec.client.name} ({spec.client.website or ''}). Offer: {spec.ask}"
    data = (f"Sender: {sender_text}\n"
            f"Recipient: {dm.first_name} {dm.last_name}, {dm.job_title} at {extraction.company_name}\n"
            f"Angle: {score.angle}\n\nVerified evidence:\n{listing}")
    signature = (sender.signature or sender.name) if sender else spec.client.name
    for _ in range(2):  # one retry if the first draft is not grounded
        out = await llm.parse(get_settings().llm_judge_model, COLD_EMAIL_RULES, data, DraftOut, effort="medium")
        if draft_is_grounded(out, listing_by_id, sender_text):
            return Draft(out.subject.strip(), f"{out.body.strip()}\n\n{signature}\n\n{OPT_OUT_LINE}",
                         out.cited_evidence_ids)
    return None
