"""Verdicts on delivered leads: saving them, summarising a run, and exporting a labelled dataset."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Company, Contact, Evidence, Job, JobLead, LeadFeedback
from app.models.enums import EvidenceKind
from app.models.lead_feedback import VERDICTS


def save(session: Session, job_lead_id: int, verdict: str, note: str | None) -> None:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    row = session.scalar(select(LeadFeedback).where(LeadFeedback.job_lead_id == job_lead_id))
    if row is None:
        row = LeadFeedback(job_lead_id=job_lead_id)
        session.add(row)
    row.verdict, row.note = verdict, (note or "").strip()[:1000] or None


def for_lead(session: Session, job_lead_id: int) -> LeadFeedback | None:
    return session.scalar(select(LeadFeedback).where(LeadFeedback.job_lead_id == job_lead_id))


def summary_for_job(session: Session, job_id: int) -> dict[str, int]:
    """{verdict: count} for one run, in VERDICTS order; empty when nothing is rated yet."""
    rows = session.execute(
        select(LeadFeedback.verdict, func.count())
        .join(JobLead, JobLead.id == LeadFeedback.job_lead_id)
        .where(JobLead.job_id == job_id)
        .group_by(LeadFeedback.verdict)
    ).all()
    counts = dict(rows)
    return {v: counts[v] for v in VERDICTS if counts.get(v)}


def verdicts_for_leads(session: Session, job_lead_ids: list[int]) -> dict[int, str]:
    if not job_lead_ids:
        return {}
    rows = session.execute(
        select(LeadFeedback.job_lead_id, LeadFeedback.verdict).where(
            LeadFeedback.job_lead_id.in_(job_lead_ids)
        )
    ).all()
    return dict(rows)


def export_rows(session: Session) -> list[dict]:
    """Every rated lead with the evidence it was delivered on: one labelled example per row."""
    out = []
    rated = session.execute(
        select(LeadFeedback, JobLead, Job, Company)
        .join(JobLead, JobLead.id == LeadFeedback.job_lead_id)
        .join(Job, Job.id == JobLead.job_id)
        .join(Company, Company.id == JobLead.company_id)
        .order_by(LeadFeedback.id)
    ).all()
    for feedback, lead, job, company in rated:
        signals = session.scalars(
            select(Evidence).where(
                Evidence.company_id == company.id,
                Evidence.job_id == job.id,
                Evidence.kind == EvidenceKind.SIGNAL,
            )
        ).all()
        contacts = session.scalars(
            select(Contact).where(Contact.company_id == company.id, Contact.email.is_not(None))
        ).all()
        out.append(
            {
                "verdict": feedback.verdict,
                "note": feedback.note,
                "job_id": job.id,
                "client": job.client_key,
                "ask": job.ask,
                "company": company.name or company.domain,
                "domain": company.domain,
                "tier": lead.lead_tier.value if lead.lead_tier else None,
                "score": {
                    "fit": lead.fit_score,
                    "intent": lead.intent_score,
                    "timing": lead.timing_score,
                    "total": lead.total_score,
                },
                "why": lead.why_summary,
                "signals": [
                    {
                        "type": e.signal_type,
                        "claim": e.claim,
                        "quote": e.quote,
                        "url": e.source_url,
                        "intent": e.intent,
                        "source_class": e.source_class,
                        "date_confidence": e.date_confidence,
                        "date": (e.event_date or e.evidence_date).isoformat()
                        if (e.event_date or e.evidence_date)
                        else None,
                    }
                    for e in signals
                ],
                "contacts": [
                    {"title": c.job_title, "role": c.buyer_role.value if c.buyer_role else None}
                    for c in contacts
                ],
            }
        )
    return out
