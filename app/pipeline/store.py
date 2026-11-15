"""Persistence for a run: companies (upsert by domain), evidence, leads, contacts, deliveries.

Writes are concurrency-safe: a get-or-create that loses a race on the unique constraint falls back
to re-reading the row another worker just inserted, instead of raising (SAVEPOINT pattern).
"""
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.countries.base import REGISTRY_MATCH_NUMBER, REGISTRY_MATCH_SOURCE
from app.models.enums import EmailVerdict, EvidenceKind, LeadStatus
from app.models.tables import Company, Contact, Delivery, Evidence, Job, JobLead, Suppression
from app.pipeline.export import LeadResult


def _get_or_create(session: Session, model, lookup: dict, **create_kwargs):
    """Find a row by `lookup`, or create it. Safe if another session inserts the same row
    concurrently: the SAVEPOINT rolls back on the unique-constraint violation and we re-read."""
    obj = session.scalar(select(model).filter_by(**lookup))
    if obj is not None:
        return obj
    try:
        with session.begin_nested():
            obj = model(**lookup, **create_kwargs)
            session.add(obj)
            session.flush()
    except IntegrityError:
        obj = session.scalar(select(model).filter_by(**lookup))
        if obj is None:
            raise  # a real constraint violation, not a concurrent-insert race
    return obj


def delivered_domains(session: Session, client_key: str) -> set[str]:
    rows = session.execute(
        select(Company.domain).join(Delivery, Delivery.company_id == Company.id).where(Delivery.client_key == client_key)
    )
    return {r[0] for r in rows}


def delivered_registry_keys(session: Session, client_key: str) -> set[tuple[str, str]]:
    """(registry, number) of every company already delivered to this client, whatever domain it came from."""
    rows = session.execute(
        select(Company.registry_name, Company.registry_id).join(Delivery, Delivery.company_id == Company.id)
        .where(Delivery.client_key == client_key, Company.registry_id.is_not(None))
    )
    return {(r[0], r[1]) for r in rows}


def suppressed_domains(session: Session) -> set[str]:
    return {r[0] for r in session.execute(select(Suppression.value).where(Suppression.kind == "domain"))}


def suppressed_emails(session: Session) -> frozenset[str]:
    return frozenset(r[0] for r in session.execute(select(Suppression.value).where(Suppression.kind == "email")))


def add_suppression(session: Session, kind: str, value: str, reason: str) -> None:
    if kind not in ("domain", "email"):
        raise ValueError(f"kind must be 'domain' or 'email', got {kind!r}")
    value = value.strip().lower()
    existing = session.scalar(select(Suppression).where(Suppression.kind == kind, Suppression.value == value))
    if existing is not None:
        existing.reason = reason
        return
    try:
        with session.begin_nested():
            session.add(Suppression(kind=kind, value=value, reason=reason))
            session.flush()
    except IntegrityError:
        pass  # suppressed concurrently by another process; the goal (it's suppressed) is already met


def erase_person_by_email(session: Session, email: str, reason: str) -> int:
    """UK GDPR / DPDP erasure: delete the person's stored contact record(s) and suppress the email
    going forward (so it can't be re-added by a later finder lookup or cache reuse)."""
    email = email.strip().lower()
    contacts = session.scalars(select(Contact).where(Contact.email == email)).all()
    for contact in contacts:
        _detach_deliveries(session, contact)
        session.delete(contact)
    add_suppression(session, "email", email, reason)
    return len(contacts)


def _detach_deliveries(session: Session, contact: Contact) -> None:
    """Keep the never-repeat record of a delivery while the person's data is erased. The FK would set
    contact_id to NULL on delete, but (client, company, NULL) is unique (NULLS NOT DISTINCT), so a second
    erased person at the same company, or an existing company-only delivery, would make the erasure fail.
    One company-level row per client is kept; the person-level rows are dropped."""
    rows = session.scalars(select(Delivery).where(Delivery.contact_id == contact.id)).all()
    for row in rows:
        company_level = session.scalar(select(Delivery.id).where(
            Delivery.client_key == row.client_key, Delivery.company_id == row.company_id,
            Delivery.contact_id.is_(None)))
        if company_level is None:
            row.contact_id = None
        else:
            session.delete(row)
        session.flush()


def upsert_company(session: Session, domain: str, country: str) -> Company:
    return _get_or_create(session, Company, {"domain": domain.strip().lower()}, country_code=country)


def _company_by_registry(session: Session, registry_name: str | None, registry_id: str | None) -> Company | None:
    if not registry_id:
        return None
    return session.scalar(select(Company).where(Company.registry_name == registry_name, Company.registry_id == registry_id))


def resolve_company(session: Session, domain: str, country: str, registry: dict) -> Company:
    """Identity resolution. A company number printed on the company's own site is identity-grade, so a
    second domain of an already-known registered company resolves to the existing record. Name-only
    registry matches never merge records (two brands can share a name). A number stated by the discovery
    source itself (an award notice) is identity-grade too, as the runner's duplicate guard already treats it."""
    if registry.get("match") in (REGISTRY_MATCH_NUMBER, REGISTRY_MATCH_SOURCE):
        if existing := _company_by_registry(session, registry.get("registry_name"), registry.get("registry_id")):
            return existing
    return upsert_company(session, domain, country)


def save_rejection(session: Session, job: Job, domain: str, country: str, reason: str, score=None) -> None:
    """Records why a candidate was rejected. If this job already has a row for the company (a second
    domain of an already-saved company, e.g. a duplicate caught by registry number), that row wins: a
    rejection must never overwrite a lead, nor raise on the (job, company) unique key and fail the job."""
    company = upsert_company(session, domain, country)
    if session.scalar(select(JobLead.id).where(JobLead.job_id == job.id, JobLead.company_id == company.id)):
        return
    lead = JobLead(job_id=job.id, company_id=company.id, status=LeadStatus.REJECTED, reject_reason=reason[:2000])
    if score is not None:  # keep the judge's reasoning for tuning the ICP/playbook
        lead.fit_score, lead.intent_score, lead.timing_score = score.fit, score.intent, score.timing
        lead.total_score, lead.why_summary, lead.risks = score.total, score.why, score.risks
    try:
        with session.begin_nested():
            session.add(lead)
            session.flush()
    except IntegrityError:
        pass  # another worker recorded this company for the job concurrently; its row stands


def known_verified_emails(domain: str, max_age_days: int = 60) -> dict[tuple[str, str], str]:
    """Emails already verified for this company recently: reused instead of paying for a new lookup."""
    from app.db import session_scope

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    with session_scope() as s:
        rows = s.execute(
            select(Contact.first_name, Contact.last_name, Contact.email).join(Company, Company.id == Contact.company_id)
            .where(Company.domain == domain, Contact.email_verdict == EmailVerdict.VALID,
                   Contact.email.is_not(None), Contact.email_verified_at >= cutoff)
        )
        return {(f.lower(), l.lower()): e for f, l, e in rows}


def save_lead(session: Session, job: Job, lead: LeadResult, country: str) -> dict:
    """Persists a successfully-processed lead immediately (durable before any later crash).
    Returns {(first, last): contact_id} for the lead's decision-makers."""
    b, e, reg = lead.bundle, lead.extraction, lead.registry
    company = resolve_company(session, b.domain, country, reg)
    holder = _company_by_registry(session, reg.get("registry_name"), reg.get("registry_id"))
    if reg and (holder is None or holder.id == company.id):
        # A name-only match to a number another record already holds is kept on the lead/export, not stored
        # here: the unique index allows one company record per registered company. A concurrent worker can
        # claim the same number between the check and the flush; then the identity stays with that row.
        try:
            with session.begin_nested():
                company.legal_name = reg.get("legal_name") or company.legal_name
                company.registry_name = reg.get("registry_name")
                company.registry_id = reg.get("registry_id")
                company.registry_entity_type = reg.get("entity_type")
                company.registry_status = reg.get("status")
                if reg.get("incorporated_on"):
                    company.incorporated_on = date.fromisoformat(reg["incorporated_on"])
                company.registry_checked_at = datetime.now(UTC)
                session.flush()
        except IntegrityError:
            session.refresh(company)  # the rolled-back savepoint left the row expired
    company.name = e.company_name
    company.last_fetched_at = datetime.now(UTC)
    if phone := next((p for p in b.phones), None):
        company.company_phone, company.company_phone_type = phone.e164, phone.phone_type

    for f in e.facts:
        session.add(Evidence(company_id=company.id, job_id=job.id, kind=EvidenceKind.FIRMOGRAPHIC, claim=f.claim,
                             source_url=f.source_url, quote=f.quote, quote_verified=True))
    for sig in e.signals:
        session.add(Evidence(
            company_id=company.id, job_id=job.id, kind=EvidenceKind.SIGNAL, signal_type=sig.signal_type, claim=sig.claim,
            source_url=sig.source_url, quote=sig.quote, quote_verified=True,
            evidence_date=sig.trusted_date.value if sig.trusted_date else None,
            date_source=sig.trusted_date.source if sig.trusted_date else None,
            date_precision=sig.trusted_date.precision if sig.trusted_date else None,
            event_date=sig.event_date, event_date_quote=sig.event_date_quote or None,
            event_key=(sig.event_key or None) and sig.event_key[:120], corroborating_urls=sig.corroborating_urls,
        ))

    contact_ids = {}
    for p in lead.people:
        contact = _get_or_create(
            session, Contact, {"company_id": company.id, "first_name": p.first_name, "last_name": p.last_name},
            job_title=p.job_title, person_source_url=p.source_url, person_source_quote=p.source_quote,
        )
        contact.job_title, contact.buyer_role = p.job_title, p.buyer_role
        contact.email_verdict = p.email_verdict
        contact.email_finder_status = p.finder_status
        if p.email:
            contact.email, contact.email_source, contact.email_verified_at = p.email, p.email_source, datetime.now(UTC)
        elif contact.email is not None:
            # A previously verified address that no longer verifies is dropped, not kept beside a non-valid
            # verdict: only currently verified emails are stored (and the DB enforces it).
            contact.email, contact.email_source, contact.email_verified_at = None, None, None
        session.flush()
        contact_ids[(p.first_name, p.last_name)] = contact.id

    sc = lead.score
    fields = dict(
        fit_score=sc.fit, intent_score=sc.intent, timing_score=sc.timing, total_score=sc.total,
        why_summary=sc.why, outreach_angle=sc.angle, risks=sc.risks, alive_proofs=lead.alive_proofs,
        outreach_allowed=lead.outreach_allowed, outreach_reason=lead.outreach_reason,
        status=LeadStatus.QUALIFIED if lead.verified_people else LeadStatus.QUALIFIED_NO_CONTACT, lead_tier=lead.tier,
        reject_reason=None,
        email_drafts=[{"contact_id": contact_ids[k], "subject": d.subject, "body": d.body, "evidence_ids": d.evidence_ids}
                      for k, d in lead.drafts.items() if k in contact_ids],
    )
    job_lead = _get_or_create(session, JobLead, {"job_id": job.id, "company_id": company.id}, **fields)
    if job_lead.status == LeadStatus.REJECTED:
        # Another domain of this company was rejected earlier in the run (row already existed): the
        # qualified result is the truth for the company, so it replaces the rejection.
        for k, v in fields.items():
            setattr(job_lead, k, v)
        session.flush()
    lead.company_id = company.id
    return contact_ids


def set_ranks(session: Session, job_id: int, company_ids_in_rank_order: list[int | None]) -> None:
    """Stores the delivered rank (1 = best) the export shows, so the database and the sheet agree."""
    for rank, company_id in enumerate(company_ids_in_rank_order, 1):
        if company_id is None:
            continue
        lead = session.scalar(select(JobLead).where(JobLead.job_id == job_id, JobLead.company_id == company_id))
        if lead is not None:
            lead.rank = rank


def mark_not_delivered(session: Session, job_id: int, company_ids: list[int | None], reason: str) -> None:
    """A lead that was saved but is not being delivered becomes a rejection with its reason, so the
    stored statuses always match what the client received."""
    for company_id in company_ids:
        if company_id is None:
            continue
        lead = session.scalar(select(JobLead).where(JobLead.job_id == job_id, JobLead.company_id == company_id))
        if lead is not None:
            lead.status, lead.reject_reason, lead.rank = LeadStatus.REJECTED, reason, None


def record_deliveries(session: Session, job: Job, delivered: list[tuple[LeadResult, dict]]) -> None:
    for lead, contact_ids in delivered:
        ids = [contact_ids[(p.first_name, p.last_name)] for p in lead.verified_people] or [None]
        for cid in ids:
            _get_or_create(session, Delivery, {"client_key": job.client_key, "company_id": lead.company_id, "contact_id": cid},
                           job_id=job.id)
