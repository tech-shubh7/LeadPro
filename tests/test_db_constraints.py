"""Proves the database itself rejects invalid data, independent of the Python layer.

Every test here uses raw SQL (UPDATE/INSERT via `text()`), bypassing the ORM's own `@validates`
normalizers and Pydantic schemas entirely, so these constraints still hold even against a bug in our
Python code or a different tool writing to the database directly.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config.database import session_scope
from app.models import Company, Contact, Delivery, Evidence, Job, JobLead
from app.models.enums import EvidenceKind, JobStatus, LeadStatus, LeadTier


def _job(s) -> Job:
    job = Job(
        client_key="k",
        client_name="n",
        ask="a",
        target_countries=["GB"],
        requested_count=3,
        cost_cap_inr=50.0,
        status=JobStatus.DONE,
    )
    s.add(job)
    s.flush()
    return job


def _company(s) -> Company:
    company = Company(domain="acme.co.uk", country_code="GB")
    s.add(company)
    s.flush()
    return company


# --- jobs --------------------------------------------------------------------------------------


def test_job_requires_a_positive_requested_count(clean_db):
    with session_scope() as s:
        job_id = _job(s).id
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE jobs SET requested_count = 0 WHERE id = :id"), {"id": job_id})


def test_job_freshness_days_must_be_in_range(clean_db):
    with session_scope() as s:
        job_id = _job(s).id
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE jobs SET freshness_days = 400 WHERE id = :id"), {"id": job_id})


def test_job_cost_cap_must_be_positive(clean_db):
    with session_scope() as s:
        job_id = _job(s).id
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE jobs SET cost_cap_inr = 0 WHERE id = :id"), {"id": job_id})


def test_job_status_rejects_a_value_not_in_the_enum(clean_db):
    with session_scope() as s:
        job_id = _job(s).id
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE jobs SET status = 'not_a_real_status' WHERE id = :id"), {"id": job_id})


# --- companies -----------------------------------------------------------------------------------


def test_company_domain_must_be_lowercase(clean_db):
    with session_scope() as s:
        company_id = _company(s).id
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE companies SET domain = 'ACME.CO.UK' WHERE id = :id"), {"id": company_id})


def test_company_country_code_must_be_two_uppercase_letters(clean_db):
    with session_scope() as s:
        company_id = _company(s).id
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE companies SET country_code = 'gb' WHERE id = :id"), {"id": company_id})


def test_company_domain_must_be_unique(clean_db):
    with session_scope() as s:
        _company(s)
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(Company(domain="acme.co.uk", country_code="US"))


# --- evidence ------------------------------------------------------------------------------------


def test_evidence_cannot_be_stored_unverified(clean_db):
    """The quote-verification gate is enforced at the database, not only in the extraction code."""
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(
            text(
                "INSERT INTO evidence (company_id, job_id, kind, claim, source_url, quote, quote_verified) "
                "VALUES (:cid, :jid, 'signal', 'claim', 'https://x', 'quote', false)"
            ),
            {"cid": company_id, "jid": job_id},
        )


def test_evidence_rows_are_deleted_when_their_company_is_deleted(clean_db):
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        s.add(
            Evidence(
                company_id=company_id,
                job_id=job_id,
                kind=EvidenceKind.FIRMOGRAPHIC,
                claim="c",
                source_url="u",
                quote="q",
                quote_verified=True,
            )
        )
    with session_scope() as s:
        s.execute(text("DELETE FROM companies WHERE id = :id"), {"id": company_id})
    with session_scope() as s:
        assert s.query(Evidence).filter(Evidence.company_id == company_id).count() == 0


# --- job_leads -----------------------------------------------------------------------------------


def test_job_lead_total_score_must_equal_the_sum_of_its_components(clean_db):
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        s.add(
            JobLead(
                job_id=job_id,
                company_id=company_id,
                fit_score=10,
                intent_score=10,
                timing_score=5,
                total_score=25,
                status=LeadStatus.QUALIFIED,
                lead_tier=LeadTier.SIGNAL,
            )
        )
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE job_leads SET total_score = 99 WHERE job_id = :jid"), {"jid": job_id})


def test_job_lead_component_scores_must_be_in_range(clean_db):
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        s.add(
            JobLead(
                job_id=job_id,
                company_id=company_id,
                fit_score=10,
                intent_score=10,
                timing_score=5,
                total_score=25,
                status=LeadStatus.QUALIFIED,
                lead_tier=LeadTier.SIGNAL,
            )
        )
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE job_leads SET fit_score = 999 WHERE job_id = :jid"), {"jid": job_id})


def test_rejected_job_lead_requires_a_reason(clean_db):
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        s.add(JobLead(job_id=job_id, company_id=company_id, status=LeadStatus.REJECTED, reject_reason="x"))
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE job_leads SET reject_reason = NULL WHERE job_id = :jid"), {"jid": job_id})


def test_non_rejected_job_lead_requires_a_score(clean_db):
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        s.add(
            JobLead(
                job_id=job_id,
                company_id=company_id,
                fit_score=10,
                intent_score=10,
                timing_score=5,
                total_score=25,
                status=LeadStatus.QUALIFIED,
                lead_tier=LeadTier.SIGNAL,
            )
        )
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE job_leads SET total_score = NULL WHERE job_id = :jid"), {"jid": job_id})


def test_job_lead_unique_per_job_and_company(clean_db):
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        s.add(JobLead(job_id=job_id, company_id=company_id, status=LeadStatus.REJECTED, reject_reason="x"))
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(JobLead(job_id=job_id, company_id=company_id, status=LeadStatus.REJECTED, reject_reason="y"))


def test_job_lead_rows_are_deleted_when_their_company_is_deleted(clean_db):
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        s.add(JobLead(job_id=job_id, company_id=company_id, status=LeadStatus.REJECTED, reject_reason="x"))
    with session_scope() as s:
        s.execute(text("DELETE FROM companies WHERE id = :id"), {"id": company_id})
    with session_scope() as s:
        assert s.query(JobLead).filter(JobLead.company_id == company_id).count() == 0


# --- contacts ------------------------------------------------------------------------------------


def _contact(s, company_id: int) -> Contact:
    contact = Contact(
        company_id=company_id,
        first_name="Jane",
        last_name="Roe",
        job_title="CEO",
        person_source_url="u",
        person_source_quote="q",
    )
    s.add(contact)
    s.flush()
    return contact


def test_contact_email_requires_a_valid_verdict(clean_db):
    """An email is never stored unless it was actually confirmed deliverable, not merely 'found'."""
    with session_scope() as s:
        company_id = _company(s).id
        _contact(s, company_id)
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(
            text(
                "UPDATE contacts SET email = 'jane@acme.co.uk', email_verdict = 'unknown' WHERE company_id = :cid"
            ),
            {"cid": company_id},
        )


def test_contact_email_requires_a_verified_at_timestamp(clean_db):
    with session_scope() as s:
        company_id = _company(s).id
        _contact(s, company_id)
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(
            text(
                "UPDATE contacts SET email = 'jane@acme.co.uk', email_verdict = 'valid', email_verified_at = NULL "
                "WHERE company_id = :cid"
            ),
            {"cid": company_id},
        )


def test_contact_email_must_be_lowercase(clean_db):
    with session_scope() as s:
        company_id = _company(s).id
        _contact(s, company_id)
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(
            text(
                "UPDATE contacts SET email = 'Jane.Roe@Acme.co.uk', email_verdict = 'valid', "
                "email_verified_at = now() WHERE company_id = :cid"
            ),
            {"cid": company_id},
        )


def test_contact_unique_per_company_and_name(clean_db):
    with session_scope() as s:
        company_id = _company(s).id
        _contact(s, company_id)
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(
            Contact(
                company_id=company_id,
                first_name="Jane",
                last_name="Roe",
                job_title="COO",
                person_source_url="u2",
                person_source_quote="q2",
            )
        )


def test_delivery_contact_id_is_cleared_not_deleted_when_the_person_is_erased(clean_db):
    """Erasing a person's contact record must not resurrect the never-repeat dedup for that company."""
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        contact_id = _contact(s, company_id).id
        s.add(Delivery(client_key="k", company_id=company_id, contact_id=contact_id, job_id=job_id))
    with session_scope() as s:
        s.execute(text("DELETE FROM contacts WHERE id = :id"), {"id": contact_id})
    with session_scope() as s:
        delivery = s.query(Delivery).filter(Delivery.company_id == company_id).one()
        assert delivery.contact_id is None  # the delivery row itself survives


# --- deliveries ------------------------------------------------------------------------------------


def test_delivery_cannot_record_the_same_company_only_delivery_twice(clean_db):
    """NULLS NOT DISTINCT: a company-level (no-contact) delivery can't be duplicated either."""
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        s.add(Delivery(client_key="k", company_id=company_id, contact_id=None, job_id=job_id))
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(
            text(
                "INSERT INTO deliveries (client_key, company_id, contact_id, job_id) VALUES ('k', :cid, NULL, :jid)"
            ),
            {"cid": company_id, "jid": job_id},
        )


# --- suppressions --------------------------------------------------------------------------------


def test_suppression_kind_must_be_domain_or_email(clean_db):
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("INSERT INTO suppressions (kind, value, reason) VALUES ('phone', 'x', 'y')"))


def test_suppression_value_must_be_lowercase(clean_db):
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("INSERT INTO suppressions (kind, value, reason) VALUES ('domain', 'ACME.CO.UK', 'y')"))


def test_suppression_unique_per_kind_and_value(clean_db):
    with session_scope() as s:
        s.execute(text("INSERT INTO suppressions (kind, value, reason) VALUES ('domain', 'acme.co.uk', 'y')"))
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("INSERT INTO suppressions (kind, value, reason) VALUES ('domain', 'acme.co.uk', 'z')"))


# --- identity, tiers, event dates (v4.3) ----------------------------------------------------------


def test_one_company_record_per_registered_company(clean_db):
    with session_scope() as s:
        s.add(
            Company(
                domain="acme.co.uk",
                country_code="GB",
                registry_name="companies_house",
                registry_id="01234567",
            )
        )
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(
            text(
                "INSERT INTO companies (domain, country_code, registry_name, registry_id, registry_raw, created_at, "
                "updated_at) VALUES ('acme.com', 'GB', 'companies_house', '01234567', '{}', now(), now())"
            )
        )


def test_many_companies_may_have_no_registry_number(clean_db):
    with session_scope() as s:  # the uniqueness is partial: companies without a registry match don't collide
        s.add_all([Company(domain="a.com", country_code="US"), Company(domain="b.com", country_code="US")])


def test_registry_number_requires_its_registry_name(clean_db):
    with session_scope() as s:
        company_id = _company(s).id
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE companies SET registry_id = '01234567' WHERE id = :id"), {"id": company_id})


def test_non_rejected_lead_requires_a_tier(clean_db):
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        s.add(
            JobLead(
                job_id=job_id,
                company_id=company_id,
                fit_score=35,
                intent_score=0,
                timing_score=5,
                total_score=40,
                status=LeadStatus.QUALIFIED,
                lead_tier=LeadTier.ICP_ONLY,
            )
        )
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE job_leads SET lead_tier = NULL WHERE job_id = :jid"), {"jid": job_id})


def test_lead_tier_rejects_a_value_not_in_the_enum(clean_db):
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
        s.add(JobLead(job_id=job_id, company_id=company_id, status=LeadStatus.REJECTED, reject_reason="x"))
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("UPDATE job_leads SET lead_tier = 'hot' WHERE job_id = :jid"), {"jid": job_id})


def test_event_date_requires_the_quoted_phrase_it_came_from(clean_db):
    with session_scope() as s:
        job_id, company_id = _job(s).id, _company(s).id
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(
            text(
                "INSERT INTO evidence (company_id, job_id, kind, claim, source_url, quote, quote_verified, event_date, "
                "corroborating_urls, created_at) VALUES (:cid, :jid, 'signal', 'c', 'https://x', 'q', true, '2025-03-31', "
                "'[]', now())"
            ),
            {"cid": company_id, "jid": job_id},
        )
