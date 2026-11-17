"""Read queries for the operator console.

Every list is paginated and every per-row aggregate is one GROUP BY over just that page's ids, so a
page costs a fixed, small number of queries however many rows it shows (no N+1 as the database grows).
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models import Company, Contact, Delivery, Evidence, Job, JobLead, Suppression
from app.models.enums import EvidenceKind, JobStatus, LeadStatus, LeadTier

PAGE_SIZE = 25
# Statuses that are not a deliverable lead: shown under Dropped, never in the lead list or its counts.
DROPPED = (LeadStatus.REJECTED, LeadStatus.DROPPED_ON_REVALIDATION)
# Work in progress. A plan awaiting the operator's approval is not: nothing runs until it is approved,
# so it must not count as 'in progress' on the dashboard or keep a progress poller spinning.
RUNNING_STATUSES = frozenset(JobStatus) - {
    JobStatus.DONE,
    JobStatus.DONE_WITH_SHORTFALL,
    JobStatus.FAILED,
    JobStatus.AWAITING_ICP_REVIEW,
}


@dataclass(frozen=True)
class Page:
    items: list
    total: int
    page: int
    size: int = PAGE_SIZE

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size))

    @property
    def first(self) -> int:
        return 0 if not self.total else (self.page - 1) * self.size + 1

    @property
    def last(self) -> int:
        return min(self.total, self.page * self.size)


def _paginate(session: Session, stmt: Select, page: int, size: int = PAGE_SIZE) -> Page:
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    page = max(1, min(page, max(1, -(-total // size))))
    rows = session.execute(stmt.limit(size).offset((page - 1) * size)).all()
    return Page(list(rows), total, page, size)


# --- dashboard ----------------------------------------------------------------------------------


@dataclass
class Totals:
    jobs: int = 0
    qualified: int = 0
    no_contact: int = 0
    icp_only: int = 0
    rejected: int = 0
    companies: int = 0
    verified_emails: int = 0
    cost_inr: float = 0.0
    suppressions: int = 0
    running: int = 0

    @property
    def researched(self) -> int:
        return self.qualified + self.no_contact + self.icp_only + self.rejected

    @property
    def contact_rate(self) -> float:
        """Share of companies that passed every check and also got a verified email. The headline
        quality number: `qualified_no_contact` is the honest denominator, not hidden."""
        passed = self.qualified + self.no_contact
        return 100.0 * self.qualified / passed if passed else 0.0

    @property
    def cost_per_lead(self) -> float:
        return self.cost_inr / self.qualified if self.qualified else 0.0


def totals(session: Session) -> Totals:
    """Two round-trips whatever the size of the database: one grouped count over job_leads, and one
    row of scalar sub-selects for the independent tables."""
    t = Totals()
    for status, tier, n in session.execute(
        select(JobLead.status, JobLead.lead_tier, func.count()).group_by(JobLead.status, JobLead.lead_tier)
    ).all():
        if status in DROPPED:
            t.rejected += n
        elif tier == LeadTier.ICP_ONLY:
            t.icp_only += n
        elif status == LeadStatus.QUALIFIED:
            t.qualified += n
        else:
            t.no_contact += n

    def count_of(model, *where):
        return select(func.count()).select_from(model).where(*where).scalar_subquery()

    t.jobs, t.running, t.companies, t.verified_emails, t.cost_inr, t.suppressions = session.execute(
        select(
            count_of(Job),
            count_of(Job, Job.status.in_(RUNNING_STATUSES)),
            count_of(Company),
            count_of(Contact, Contact.email.is_not(None)),
            select(func.coalesce(func.sum(Job.cost_inr), 0.0)).scalar_subquery(),
            count_of(Suppression),
        )
    ).one()
    return t


def jobs_page(session: Session, page: int, q: str = "", status: str = "") -> Page:
    stmt = select(Job).order_by(Job.id.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Job.client_name.ilike(like), Job.client_key.ilike(like), Job.ask.ilike(like)))
    if status:
        stmt = stmt.where(Job.status == status)
    result = _paginate(session, stmt, page)
    jobs = [row[0] for row in result.items]
    counts = lead_counts_for_jobs(session, [j.id for j in jobs])
    return Page([(j, counts.get(j.id, LeadCounts())) for j in jobs], result.total, result.page, result.size)


@dataclass
class LeadCounts:
    qualified: int = 0
    no_contact: int = 0
    icp_only: int = 0
    rejected: int = 0

    @property
    def delivered(self) -> int:
        return self.qualified + self.icp_only


def lead_counts_for_jobs(session: Session, job_ids: list[int]) -> dict[int, LeadCounts]:
    """One GROUP BY for the whole page of jobs."""
    if not job_ids:
        return {}
    rows = session.execute(
        select(JobLead.job_id, JobLead.status, JobLead.lead_tier, func.count())
        .where(JobLead.job_id.in_(job_ids))
        .group_by(JobLead.job_id, JobLead.status, JobLead.lead_tier)
    ).all()
    out: dict[int, LeadCounts] = {job_id: LeadCounts() for job_id in job_ids}
    for job_id, status, tier, n in rows:
        c = out[job_id]
        if status in DROPPED:
            c.rejected += n
        elif tier == LeadTier.ICP_ONLY:
            c.icp_only += n
        elif status == LeadStatus.QUALIFIED:
            c.qualified += n
        else:
            c.no_contact += n
    return out


# --- one job ------------------------------------------------------------------------------------


@dataclass
class LeadRow:
    lead: JobLead
    company: Company
    contacts: list[Contact] = field(default_factory=list)
    signals: int = 0
    latest_signal: date | None = None

    @property
    def verified(self) -> list[Contact]:
        return [c for c in self.contacts if c.email]


def job_leads_page(
    session: Session, job_id: int, page: int, tier: str = "", contact: str = "", q: str = ""
) -> Page:
    stmt = (
        select(JobLead, Company)
        .join(Company, Company.id == JobLead.company_id)
        .where(JobLead.job_id == job_id, JobLead.status.not_in(DROPPED))
        .order_by(JobLead.lead_tier, JobLead.rank.is_(None), JobLead.rank, JobLead.total_score.desc())
    )
    if tier in (LeadTier.SIGNAL, LeadTier.ICP_ONLY):
        stmt = stmt.where(JobLead.lead_tier == tier)
    if contact == "yes":
        stmt = stmt.where(JobLead.status == LeadStatus.QUALIFIED)
    elif contact == "no":
        stmt = stmt.where(JobLead.status == LeadStatus.QUALIFIED_NO_CONTACT)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Company.name.ilike(like), Company.domain.ilike(like), Company.legal_name.ilike(like))
        )
    result = _paginate(session, stmt, page)
    rows = [LeadRow(lead, company) for lead, company in result.items]
    _attach_contacts_and_signals(session, job_id, rows)
    return Page(rows, result.total, result.page, result.size)


def _attach_contacts_and_signals(session: Session, job_id: int, rows: list[LeadRow]) -> None:
    """Two queries for the page: contacts for its companies, and signal counts/latest dates."""
    company_ids = [r.company.id for r in rows]
    if not company_ids:
        return
    contacts: dict[int, list[Contact]] = {}
    for c in session.scalars(
        select(Contact)
        .where(Contact.company_id.in_(company_ids))
        .order_by(Contact.email.is_(None), Contact.id)
    ):
        contacts.setdefault(c.company_id, []).append(c)
    stats = session.execute(
        select(
            Evidence.company_id,
            func.count(),
            func.max(func.coalesce(Evidence.event_date, Evidence.evidence_date)),
        )
        .where(
            Evidence.company_id.in_(company_ids),
            Evidence.job_id == job_id,
            Evidence.kind == EvidenceKind.SIGNAL,
        )
        .group_by(Evidence.company_id)
    ).all()
    by_company = {cid: (n, latest) for cid, n, latest in stats}
    for r in rows:
        r.contacts = contacts.get(r.company.id, [])
        r.signals, r.latest_signal = by_company.get(r.company.id, (0, None))


def job_rejections_page(session: Session, job_id: int, page: int) -> Page:
    stmt = (
        select(JobLead, Company)
        .join(Company, Company.id == JobLead.company_id)
        .where(JobLead.job_id == job_id, JobLead.status.in_(DROPPED))
        .order_by(JobLead.id)
    )
    return _paginate(session, stmt, page)


def rejection_reasons(session: Session, job: Job) -> list[tuple[str, int]]:
    """Grouped funnel. Uses the job's recorded tally when present (it keeps the grouping the run used),
    otherwise groups the stored rows."""
    recorded = (job.progress or {}).get("rejected") or {}
    if recorded:
        return sorted(recorded.items(), key=lambda kv: -kv[1])
    rows = session.execute(
        select(JobLead.reject_reason, func.count())
        .where(JobLead.job_id == job.id, JobLead.status.in_(DROPPED))
        .group_by(JobLead.reject_reason)
    ).all()
    tally: dict[str, int] = {}
    for reason, n in rows:
        key = (reason or "unknown").split(":")[0].split(" (")[0][:60]
        tally[key] = tally.get(key, 0) + n
    return sorted(tally.items(), key=lambda kv: -kv[1])


def cost_breakdown(job: Job) -> list[tuple[str, int, float]]:
    """(provider/operation, calls, ₹) from the job's recorded usage lines."""
    tally: dict[str, list] = {}
    for line in job.cost_lines or []:
        key = f"{line.get('provider', '?')} · {line.get('operation', '?')}"
        entry = tally.setdefault(key, [0, 0.0])
        entry[0] += 1
        entry[1] += float(line.get("cost_inr") or 0.0)
    return sorted(((k, v[0], v[1]) for k, v in tally.items()), key=lambda r: -r[2])


# --- one lead -----------------------------------------------------------------------------------


@dataclass
class LeadDetail:
    lead: JobLead
    job: Job
    company: Company
    contacts: list[Contact]
    signals: list[Evidence]
    facts: list[Evidence]
    deliveries: list[Delivery]

    @property
    def drafts_by_contact(self) -> dict[int, dict]:
        return {d.get("contact_id"): d for d in (self.lead.email_drafts or []) if d.get("contact_id")}


def lead_detail(session: Session, lead_id: int) -> LeadDetail | None:
    row = session.execute(
        select(JobLead, Job, Company)
        .join(Job, Job.id == JobLead.job_id)
        .join(Company, Company.id == JobLead.company_id)
        .where(JobLead.id == lead_id)
    ).first()
    if row is None:
        return None
    lead, job, company = row
    evidence = list(
        session.scalars(
            select(Evidence)
            .where(Evidence.company_id == company.id, Evidence.job_id == job.id)
            .order_by(
                func.coalesce(Evidence.event_date, Evidence.evidence_date).desc().nullslast(), Evidence.id
            )
        )
    )
    contacts = list(
        session.scalars(
            select(Contact)
            .where(Contact.company_id == company.id)
            .order_by(Contact.email.is_(None), Contact.id)
        )
    )
    deliveries = list(
        session.scalars(
            select(Delivery)
            .where(Delivery.company_id == company.id, Delivery.client_key == job.client_key)
            .order_by(Delivery.delivered_at.desc())
        )
    )
    return LeadDetail(
        lead,
        job,
        company,
        contacts,
        [e for e in evidence if e.kind == EvidenceKind.SIGNAL],
        [e for e in evidence if e.kind == EvidenceKind.FIRMOGRAPHIC],
        deliveries,
    )


# --- suppressions -------------------------------------------------------------------------------


def suppressions_page(session: Session, page: int, q: str = "") -> Page:
    stmt = select(Suppression).order_by(Suppression.id.desc())
    if q:
        stmt = stmt.where(Suppression.value.ilike(f"%{q.strip().lower()}%"))
    result = _paginate(session, stmt, page)
    return Page([row[0] for row in result.items], result.total, result.page, result.size)


# --- live progress ------------------------------------------------------------------------------


def job_progress(session: Session, job_id: int) -> dict[str, Any] | None:
    row = session.execute(
        select(Job.status, Job.progress, Job.cost_inr, Job.finished_at).where(Job.id == job_id)
    ).first()
    if row is None:
        return None
    status, progress, cost, finished_at = row
    counts = lead_counts_for_jobs(session, [job_id]).get(job_id, LeadCounts())
    return {
        "status": status.value,
        "running": status in RUNNING_STATUSES,
        "candidates": (progress or {}).get("candidates", 0),
        "qualified": counts.qualified,
        "icp_only": counts.icp_only,
        "no_contact": counts.no_contact,
        "rejected": counts.rejected,
        "cost_inr": round(float(cost or 0.0), 2),
        "finished_at": finished_at.isoformat() if isinstance(finished_at, datetime) else None,
    }


# --- single rows for the routes ---------------------------------------------------------------


def get_job(session: Session, job_id: int) -> Job | None:
    return session.get(Job, job_id)


def load_job(job_id: int) -> Job | None:
    """With its own short session: for async handlers that hop to the threadpool for one read."""
    from app.config.database import SessionLocal

    with SessionLocal() as session:
        return session.get(Job, job_id)


def draft_status(session: Session, job_id: int) -> dict | None:
    """Cheap poll target while a plan drafts: two columns, no ORM object."""
    row = session.execute(select(Job.status, Job.error).where(Job.id == job_id)).first()
    if row is None:
        return None
    return {"status": row[0].value, "ready": row[0] != JobStatus.RESEARCHING_CLIENT, "error": row[1]}
