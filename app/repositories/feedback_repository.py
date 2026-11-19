"""Verdicts on delivered leads: saving them, summarising a run, and exporting a labelled dataset."""

from dataclasses import dataclass, field

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


# --- score calibration ----------------------------------------------------------------------------------

# Below this many rated leads a suggestion would be noise: the report says how many more are needed instead.
MIN_RATED = 20
MIN_ABOVE = 10  # rated leads at or above a candidate threshold before it may be suggested
TARGET_PRECISION = 0.7  # share of leads at or above the bar a person should rate "good"
BANDS = ((80, 100, "80+"), (70, 79, "70-79"), (60, 69, "60-69"), (55, 59, "55-59"), (0, 54, "under 55"))
CANDIDATE_THRESHOLDS = (80, 75, 70, 65, 60, 55)


@dataclass(frozen=True)
class Band:
    label: str
    good: int = 0
    wrong_company: int = 0
    wrong_intent: int = 0
    wrong_contact: int = 0  # a contact problem, not a scoring one: left out of precision

    @property
    def judged(self) -> int:
        return self.good + self.wrong_company + self.wrong_intent

    @property
    def precision(self) -> float | None:
        return self.good / self.judged if self.judged else None


@dataclass
class Calibration:
    bands: list[Band]
    rated: int
    threshold: int
    suggested: int | None = None
    notes: list[str] = field(default_factory=list)


def calibration(session: Session, threshold: int) -> Calibration:
    """How well the score predicts what a person rates good. Ratings on watchlist leads (55-69) are what
    can justify lowering the bar; ratings on qualified leads are what can justify raising it. Suggests a
    threshold only with enough ratings, and never changes anything itself."""
    rows = session.execute(
        select(JobLead.total_score, LeadFeedback.verdict)
        .join(LeadFeedback, LeadFeedback.job_lead_id == JobLead.id)
        .where(JobLead.total_score.is_not(None))
    ).all()
    tallies = {label: dict.fromkeys(VERDICTS, 0) for *_, label in BANDS}
    for total, verdict in rows:
        label = next(label for low, high, label in BANDS if low <= total <= high)
        tallies[label][verdict] += 1
    bands = [Band(label, **tallies[label]) for *_, label in BANDS]
    report = Calibration(bands, len(rows), threshold)
    if report.rated < MIN_RATED:
        report.notes.append(
            f"{report.rated} of {MIN_RATED} rated leads needed before a threshold can be suggested. Rate "
            "leads on their lead page (Good / Wrong company / Wrong intent / Wrong contact), including "
            "Watchlist leads: they show whether the bar is too high."
        )
        return report

    def precision_at(t: int) -> tuple[int, float | None]:
        above = [v for total, v in rows if total >= t and v != "wrong_contact"]
        return len(above), (sum(v == "good" for v in above) / len(above) if above else None)

    for t in sorted(CANDIDATE_THRESHOLDS):  # lowest bar that still meets the target, with enough evidence
        n, p = precision_at(t)
        if n >= MIN_ABOVE and p is not None and p >= TARGET_PRECISION:
            report.suggested = t
            break
    n, p = precision_at(threshold)
    if p is not None:
        report.notes.append(f"At the current bar ({threshold}), {p:.0%} of {n} rated leads were good.")
    if report.suggested is None:
        report.notes.append(
            f"No bar reaches {TARGET_PRECISION:.0%} good with {MIN_ABOVE}+ rated leads: the score is not "
            "separating good from bad yet. Look at the wrong-company and wrong-intent counts below."
        )
    elif report.suggested != threshold:
        n2, p2 = precision_at(report.suggested)
        report.notes.append(
            f"A bar of {report.suggested} would have kept {p2:.0%} good across {n2} rated leads. Change "
            "QUALIFY_SCORE (and WATCHLIST_MIN_SCORE) in .env to apply it."
        )
    wrong_company = sum(b.wrong_company for b in bands)
    wrong_intent = sum(b.wrong_intent for b in bands)
    if wrong_company > wrong_intent and wrong_company >= 3:
        report.notes.append(
            f"Most misses are the wrong company ({wrong_company}): the fit score is too generous. Tighten "
            "the ICP and its disqualifiers before touching the bar."
        )
    elif wrong_intent > wrong_company and wrong_intent >= 3:
        report.notes.append(
            f"Most misses are the wrong intent ({wrong_intent}): the need score is too generous. Review the "
            "signal playbook and which signs count as demand."
        )
    return report
