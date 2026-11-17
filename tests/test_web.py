"""Operator console: routes, the counting contract, the security controls and the no-N+1 guarantee."""

import logging
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from app.config.database import session_scope
from app.config.settings import get_settings
from app.main import app, create_app
from app.models import Company, Contact, Evidence, Job, JobLead
from app.models.enums import (
    BuyerRole,
    DateSource,
    EmailSource,
    EmailVerdict,
    EvidenceKind,
    JobStatus,
    LeadStatus,
    LeadTier,
)
from app.repositories import console_repository as q
from app.services import run_manager_service as runs

TODAY = date(2026, 11, 11)


@pytest.fixture
def client():
    return TestClient(app)


def _seed(
    n_signal: int = 2, n_icp: int = 1, n_no_contact: int = 1, n_rejected: int = 1, prefix: str = "a"
) -> int:
    """A job with one lead of each kind, so the console's four buckets are all exercised.
    `prefix` keeps domains unique when one test seeds more than one job."""
    with session_scope() as s:
        job = Job(
            client_key="acme-co",
            client_name="Acme Co",
            ask="30 UK 3PLs hiring warehouse ops",
            target_countries=["GB"],
            requested_count=30,
            cost_cap_inr=50.0,
            cost_inr=12.5,
            status=JobStatus.DONE,
            personalized_email=True,
            icp={
                "icp": {"industries": ["logistics"]},
                "playbook": {
                    "signals": [{"type": "hiring", "description": "d", "strength": "high"}],
                    "buyer_titles": ["operations director"],
                },
            },
            cost_lines=[{"provider": "serper", "operation": "search", "units": 1, "cost_inr": 0.09}],
            progress={"candidates": 9, "rejected": {"no verified signal": n_rejected}},
        )
        s.add(job)
        s.flush()
        made = 0

        def lead(domain: str, status: LeadStatus, tier: LeadTier | None, with_email: bool, rank: int | None):
            nonlocal made
            made += 1
            company = Company(
                domain=domain,
                country_code="GB",
                name=domain.split(".")[0].title(),
                registry_name="companies_house",
                registry_id=f"{prefix}000000{made}",
                registry_status="active",
                registry_entity_type="ltd",
            )
            s.add(company)
            s.flush()
            s.add(
                JobLead(
                    job_id=job.id,
                    company_id=company.id,
                    status=status,
                    lead_tier=tier,
                    rank=rank,
                    fit_score=34,
                    intent_score=30,
                    timing_score=14,
                    total_score=78,
                    why_summary="Hiring warehouse ops [E1]",
                    outreach_angle="Mention the Leeds site",
                    risks="None found",
                    alive_proofs=["website_live", "registry_active"],
                    outreach_allowed=True,
                    outreach_reason="UK: active ltd",
                    email_drafts=[],
                )
            )
            s.add(
                Evidence(
                    company_id=company.id,
                    job_id=job.id,
                    kind=EvidenceKind.SIGNAL,
                    signal_type="hiring",
                    claim="Hiring three warehouse operatives",
                    source_url=f"https://{domain}/news/2026/10/x",
                    quote="we are hiring three warehouse operatives in Leeds",
                    quote_verified=True,
                    evidence_date=date(2026, 10, 20),
                    date_source=DateSource.ARTICLE_META,
                )
            )
            s.add(
                Evidence(
                    company_id=company.id,
                    job_id=job.id,
                    kind=EvidenceKind.FIRMOGRAPHIC,
                    claim="Third-party logistics provider",
                    source_url=f"https://{domain}/about",
                    quote="we are a third-party logistics provider based in Leeds",
                    quote_verified=True,
                )
            )
            contact = Contact(
                company_id=company.id,
                first_name="Dana",
                last_name=f"Lee{made}",
                job_title="Operations Director",
                buyer_role=BuyerRole.ECONOMIC_BUYER,
                person_source_url=f"https://{domain}/team",
                person_source_quote=f"Dana Lee{made}, Operations Director",
                email_verdict=EmailVerdict.VALID if with_email else EmailVerdict.UNKNOWN,
            )
            if with_email:
                contact.email = f"dana{made}@{domain}"
                contact.email_source = EmailSource.FINDER
                contact.email_verified_at = datetime.now(UTC)
            s.add(contact)

        for i in range(n_signal):
            lead(f"{prefix}-signal{i}.co.uk", LeadStatus.QUALIFIED, LeadTier.SIGNAL, True, i + 1)
        for i in range(n_icp):
            lead(f"{prefix}-icp{i}.co.uk", LeadStatus.QUALIFIED, LeadTier.ICP_ONLY, True, i + 1)
        for i in range(n_no_contact):
            lead(
                f"{prefix}-nocontact{i}.co.uk", LeadStatus.QUALIFIED_NO_CONTACT, LeadTier.SIGNAL, False, None
            )
        for i in range(n_rejected):
            made += 1
            company = Company(domain=f"{prefix}-rejected{i}.co.uk", country_code="GB")
            s.add(company)
            s.flush()
            s.add(
                JobLead(
                    job_id=job.id,
                    company_id=company.id,
                    status=LeadStatus.REJECTED,
                    reject_reason="no verified signal dated within 90 days",
                )
            )
        return job.id


# --- routes -------------------------------------------------------------------------------------


def test_healthz_reports_the_database(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_every_page_renders_with_real_rows(clean_db, client):
    job_id = _seed()
    with session_scope() as s:
        lead_id = s.query(JobLead).filter(JobLead.status != LeadStatus.REJECTED).first().id

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Acme Co" in dashboard.text
    for url in (
        f"/jobs/{job_id}",
        f"/jobs/{job_id}?tab=rejected",
        f"/jobs/{job_id}?tab=method",
        f"/leads/{lead_id}",
        "/suppressions",
    ):
        assert client.get(url).status_code == 200, url
    assert client.get("/jobs/99999").status_code == 404
    assert client.get("/leads/99999").status_code == 404


def test_lead_page_shows_the_evidence_chain(clean_db, client):
    _seed(n_signal=1, n_icp=0, n_no_contact=0, n_rejected=0)
    with session_scope() as s:
        lead_id = s.query(JobLead).first().id
    page = client.get(f"/leads/{lead_id}").text
    assert "we are hiring three warehouse operatives in Leeds" in page  # the verbatim quote
    assert "Quote verified" in page
    assert "Article meta" in page
    assert "Dana" in page
    assert "Operations Director" in page


def test_filters_narrow_the_lead_list(clean_db, client):
    job_id = _seed(n_signal=1, n_icp=1, n_no_contact=1, n_rejected=0)
    assert "a-icp0.co.uk" not in client.get(f"/jobs/{job_id}?tier=signal").text
    assert "a-signal0.co.uk" not in client.get(f"/jobs/{job_id}?tier=icp_only").text
    assert "a-nocontact0.co.uk" not in client.get(f"/jobs/{job_id}?contact=yes").text
    only = client.get(f"/jobs/{job_id}?q=a-nocontact0").text
    assert "a-nocontact0.co.uk" in only
    assert "a-signal0.co.uk" not in only


def test_progress_endpoint_matches_the_stored_rows(clean_db, client):
    job_id = _seed()
    body = client.get(f"/jobs/{job_id}/progress").json()
    assert body == {
        "status": "done",
        "running": False,
        "candidates": 9,
        "qualified": 2,
        "icp_only": 1,
        "no_contact": 1,
        "rejected": 1,
        "cost_inr": 12.5,
        "finished_at": None,
    }


# --- the counting contract ----------------------------------------------------------------------


def test_dashboard_and_job_pages_count_the_same_way(clean_db):
    """An ICP-only company without a verified email belongs to exactly one bucket: it used to be
    counted both as ICP-only and as 'no verified contact', inflating the totals."""
    job_id = _seed(n_signal=2, n_icp=1, n_no_contact=1, n_rejected=3)
    with session_scope() as s:  # an ICP-only lead that never got an email
        company = Company(domain="icp-nocontact.co.uk", country_code="GB")
        s.add(company)
        s.flush()
        s.add(
            JobLead(
                job_id=job_id,
                company_id=company.id,
                status=LeadStatus.QUALIFIED_NO_CONTACT,
                lead_tier=LeadTier.ICP_ONLY,
                fit_score=33,
                intent_score=2,
                timing_score=5,
                total_score=40,
            )
        )
    with session_scope() as s:
        t = q.totals(s)
        per_job = q.lead_counts_for_jobs(s, [job_id])[job_id]
    assert (t.qualified, t.icp_only, t.no_contact, t.rejected) == (2, 2, 1, 3)
    assert (per_job.qualified, per_job.icp_only, per_job.no_contact, per_job.rejected) == (2, 2, 1, 3)
    assert t.contact_rate == pytest.approx(100 * 2 / 3)


def test_page_cost_does_not_grow_with_the_number_of_leads(clean_db, client):
    """The no-N+1 guarantee: the same page over 3 leads and over 18 leads issues the same queries."""

    class Counter(logging.Handler):
        def __init__(self):
            super().__init__()
            self.n = 0

        def emit(self, record):
            if str(record.msg).strip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
                self.n += 1

    engine_log = logging.getLogger("sqlalchemy.engine.Engine")
    engine_log.setLevel(logging.INFO)
    counter = Counter()
    engine_log.addHandler(counter)
    try:
        small = _seed(n_signal=2, n_icp=1, n_no_contact=0, n_rejected=0, prefix="small")
        client.get(f"/jobs/{small}")  # warm up any one-off work
        counter.n = 0
        client.get(f"/jobs/{small}")
        few = counter.n
        big = _seed(n_signal=14, n_icp=2, n_no_contact=2, n_rejected=0, prefix="big")
        counter.n = 0
        client.get(f"/jobs/{big}")
        many = counter.n
    finally:
        engine_log.removeHandler(counter)
    assert few == many, f"{few} queries for 3 leads but {many} for 18: an N+1 crept in"
    assert many <= 8, f"{many} queries for one page is more than this page needs"


# --- security -----------------------------------------------------------------------------------


def test_responses_carry_the_security_headers(client):
    headers = client.get("/").headers
    assert "unsafe-inline" not in headers["content-security-policy"]
    assert headers["content-security-policy"].startswith("default-src 'self'")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["cache-control"] == "no-store"  # pages hold personal data: never cached
    assert "immutable" in client.get("/static/app.css").headers["cache-control"]


def test_state_changing_posts_need_the_csrf_token(clean_db, client):
    form = {"kind": "email", "value": "someone@example.co.uk", "reason": "opt-out"}
    assert client.post("/suppressions", data=form).status_code == 400
    client.get("/suppressions")  # issues the cookie
    token = client.cookies.get("lp_csrf")
    assert (
        client.post("/suppressions", data=form | {"csrf": token}, follow_redirects=False).status_code == 303
    )
    assert client.post("/suppressions", data=form | {"csrf": "forged"}).status_code == 400


def test_suppression_and_erasure_write_through_the_console(clean_db, client):
    _seed(n_signal=1, n_icp=0, n_no_contact=0, n_rejected=0)
    with session_scope() as s:
        email = s.query(Contact).filter(Contact.email.is_not(None)).first().email
    client.get("/suppressions")
    token = client.cookies.get("lp_csrf")
    client.post("/suppressions/erase", data={"email": email, "reason": "erasure request", "csrf": token})
    with session_scope() as s:
        assert s.query(Contact).filter(Contact.email == email).count() == 0
    assert email in client.get("/suppressions").text


def test_downloads_cannot_escape_the_output_directory(clean_db, client):
    job_id = _seed(n_signal=1, n_icp=0, n_no_contact=0, n_rejected=0)
    assert client.get(f"/jobs/{job_id}/files/..%2f..%2f.env").status_code in (404, 422)
    assert client.get(f"/jobs/{job_id}/files/nothing_like_this.csv").status_code == 404


def test_a_token_closes_the_console(clean_db):
    settings = get_settings()
    settings.web_auth_token = "s3cret"
    try:
        guarded = TestClient(create_app())
        assert guarded.get("/", follow_redirects=False).status_code == 401
        assert guarded.get("/?token=wrong", follow_redirects=False).status_code == 401
        assert guarded.get("/healthz").status_code == 200  # probes stay open
        signed_in = guarded.get("/?token=s3cret", follow_redirects=False)
        assert signed_in.status_code == 303
        assert "lp_auth" in signed_in.headers["set-cookie"]
        from app.middlewares.auth import session_value as _session_value

        assert "s3cret" not in signed_in.headers["set-cookie"]  # the cookie never holds the key itself
        guarded.cookies.set("lp_auth", _session_value("s3cret"))
        assert guarded.get("/").status_code == 200
    finally:
        settings.web_auth_token = ""


# --- intake: ask -> drafted plan -> review -> start ---------------------------------------------


def test_intake_rejects_a_bad_ask_without_creating_anything(clean_db, client):
    client.get("/new")
    token = client.cookies.get("lp_csrf")
    before = _job_count()
    res = client.post(
        "/new", data={"website": "not a domain", "ask": "short", "count": 0, "cost_cap_inr": 0, "csrf": token}
    )
    assert res.status_code == 400
    assert "doesn&#39;t look like a website" in res.text or "doesn't look like a website" in res.text
    assert _job_count() == before  # nothing created, nothing spent


def test_intake_creates_a_job_awaiting_its_plan(clean_db, client, monkeypatch):
    """The ask is stored and drafting is handed to the background; the request itself does no LLM work."""
    started: list[int] = []
    monkeypatch.setattr(runs, "start_draft", started.append)
    client.get("/new")
    token = client.cookies.get("lp_csrf")
    res = client.post(
        "/new",
        data={
            "website": "https://www.acme.co.uk/about",
            "ask": "UK 3PLs hiring warehouse operatives",
            "count": 12,
            "country": "GB",
            "freshness_days": 60,
            "cost_cap_inr": 40,
            "personalized_email": "true",
            "csrf": token,
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    job_id = int(res.headers["location"].rsplit("/", 1)[1])
    assert started == [job_id]
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.RESEARCHING_CLIENT
        assert job.client_key == "acme-co-uk"
        assert job.client_website == "https://www.acme.co.uk/about"
        assert (job.requested_count, job.freshness_days, job.cost_cap_inr) == (12, 60, 40.0)
        assert job.personalized_email is True
    assert "Reading acme.co.uk" in client.get(f"/new/{job_id}").text  # waiting screen
    assert client.get(f"/new/{job_id}/status").json() == {
        "status": "researching_client",
        "ready": False,
        "error": None,
    }


def _parked_job(ask: str = "UK 3PLs hiring warehouse operatives") -> int:
    """A job sitting at the review step, with a plan built locally (no LLM call)."""
    from app.services.intake_service import fallback_plan, plan_to_json

    plan = fallback_plan(ask, 10, "GB", "acme.co.uk", "Acme Logistics", "test")
    plan.cost_cap_inr = 40.0
    with session_scope() as s:
        job = Job(
            client_key="acme-co-uk",
            client_name="Acme Logistics",
            client_website="acme.co.uk",
            ask=ask,
            target_countries=["GB"],
            requested_count=10,
            cost_cap_inr=40.0,
            freshness_days=90,
            icp=plan_to_json(plan),
            status=JobStatus.AWAITING_ICP_REVIEW,
        )
        s.add(job)
        s.flush()
        return job.id


def test_the_plan_is_shown_for_review_before_anything_is_spent(clean_db, client):
    job_id = _parked_job()
    page = client.get(f"/new/{job_id}").text
    assert 'name="signal_queries"' in page
    assert 'name="buyer_titles"' in page
    assert "Generic template" in page  # the fallback is labelled, never passed off as research
    assert "Start research" in page


def test_starting_a_run_uses_the_edited_plan(clean_db, client, monkeypatch):
    launched: list[tuple[int, object]] = []
    monkeypatch.setattr(runs, "start_run", lambda job_id, spec: launched.append((job_id, spec)))
    job_id = _parked_job()
    client.get(f"/new/{job_id}")
    token = client.cookies.get("lp_csrf")
    res = client.post(
        f"/new/{job_id}/start",
        data={
            "csrf": token,
            "ask": "UK 3PLs opening a new site",
            "offer": "fulfilment software",
            "count": 7,
            "freshness_days": 45,
            "cost_cap_inr": 30,
            "personalized_email": "true",
            "industries": "logistics, 3pl",
            "geographies": "United Kingdom",
            "size_band": "20-200 staff",
            "business_model": "3PL",
            "why_they_buy": "new site means new systems",
            "disqualifiers": "already has a WMS",
            "buyer_titles": "operations director, founder",
            "negative": "built it in-house",
            "why_choose": "ten years in fulfilment",
            "signal_type": ["new_site", "dropped_one"],
            "signal_description": ["Opening a new warehouse", "no queries so it cannot be searched"],
            "signal_strength": ["very_high", "medium"],
            "signal_kind": ["news", "search"],
            "signal_queries": ['"opens new warehouse" UK 3PL\nnew distribution centre opening UK', "  "],
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == f"/jobs/{job_id}"
    assert len(launched) == 1
    sent_id, spec = launched[0]
    assert sent_id == job_id
    assert spec.client.key == "acme-co-uk"  # never-repeat stays tied to the client, not the run
    assert (spec.count, spec.freshness_days, spec.cost_cap_inr) == (7, 45, 30.0)
    assert spec.ask == "UK 3PLs opening a new site"
    assert spec.icp["industries"] == ["logistics", "3pl"]
    assert [s.type for s in spec.playbook.signals] == ["new_site"]  # the query-less signal was dropped
    assert spec.playbook.signals[0].kind == "news"
    assert spec.playbook.signals[0].strength == "very_high"
    assert len(spec.playbook.signals[0].queries) == 2
    assert spec.playbook.buyer_titles == ["operations director", "founder"]
    assert spec.sender.why_choose == ["ten years in fulfilment"]


def test_a_plan_with_no_searchable_signal_is_refused(clean_db, client, monkeypatch):
    monkeypatch.setattr(runs, "start_run", lambda *a: pytest.fail("must not start"))
    job_id = _parked_job()
    client.get(f"/new/{job_id}")
    token = client.cookies.get("lp_csrf")
    res = client.post(
        f"/new/{job_id}/start",
        data={"csrf": token, "buyer_titles": "founder", "signal_type": [""], "signal_queries": [""]},
    )
    assert res.status_code == 400
    assert "signal" in res.text.lower()


def test_a_run_started_while_others_are_going_is_queued_not_refused(clean_db, client, monkeypatch):
    """Runs used to be refused (429) past two at once; now they wait in the queue for a worker."""
    monkeypatch.setattr(runs, "active_runs", lambda: runs.MAX_CONCURRENT_RUNS)
    job_id = _parked_job()
    client.get(f"/new/{job_id}")
    token = client.cookies.get("lp_csrf")
    res = client.post(
        f"/new/{job_id}/start",
        data={"csrf": token, "buyer_titles": "founder", "signal_type": ["hiring"], "signal_queries": ["q"]},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.QUEUED
        assert job.run_spec["playbook"]["signals"][0]["type"] == "hiring"


def test_a_finished_run_does_not_go_back_to_the_wizard(clean_db, client):
    job_id = _seed()
    res = client.get(f"/new/{job_id}", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == f"/jobs/{job_id}"


def _job_count() -> int:
    with session_scope() as s:
        return s.query(Job).count()


# --- regressions found in the 2026-11-14 pass ---------------------------------------------------


def test_a_first_time_visitor_can_submit_the_first_form(clean_db, monkeypatch):
    """The token in the form must match the cookie set on that same first response."""
    monkeypatch.setattr(runs, "start_draft", lambda job_id: None)
    fresh = TestClient(app)
    page = fresh.get("/new")
    cookie = fresh.cookies.get("lp_csrf")
    assert cookie
    assert f'name="csrf" value="{cookie}"' in page.text
    res = fresh.post(
        "/new",
        data={
            "csrf": cookie,
            "website": "acme.co.uk",
            "count": 5,
            "country": "GB",
            "ask": "UK 3PLs hiring warehouse staff",
            "cost_cap_inr": 50,
        },
        follow_redirects=False,
    )
    assert res.status_code == 303


def test_refusals_are_pages_not_json(clean_db, client):
    job_id = _parked_job()
    client.get(f"/new/{job_id}")
    res = client.post(f"/new/{job_id}/start", data={"csrf": "wrong"})
    assert res.status_code == 400
    assert "<!doctype html>" in res.text
    assert '"detail"' not in res.text
    bad_number = client.post("/new", data={"csrf": client.cookies.get("lp_csrf"), "count": "lots"})
    assert bad_number.status_code == 400
    assert "<!doctype html>" in bad_number.text
    polled = client.get("/jobs/999999/progress", headers={"Accept": "application/json"})
    assert polled.status_code == 404
    assert polled.json()["detail"]


def test_a_started_run_cannot_be_started_twice(clean_db, client, monkeypatch):
    monkeypatch.setattr(runs, "start_run", lambda *a: pytest.fail("must not start a second copy"))
    job_id = _seed()  # already done
    client.get("/")
    res = client.post(
        f"/new/{job_id}/start",
        data={
            "csrf": client.cookies.get("lp_csrf"),
            "buyer_titles": "founder",
            "signal_type": ["hiring"],
            "signal_queries": ["q"],
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == f"/jobs/{job_id}"


def test_a_run_orphaned_by_a_restart_stops_showing_as_running(clean_db):
    from datetime import timedelta

    with session_scope() as s:
        old = Job(
            client_key="k",
            client_name="Old",
            ask="a",
            target_countries=["GB"],
            requested_count=5,
            cost_cap_inr=10,
            status=JobStatus.RESEARCHING,
        )
        fresh = Job(
            client_key="k",
            client_name="Fresh",
            ask="a",
            target_countries=["GB"],
            requested_count=5,
            cost_cap_inr=10,
            status=JobStatus.RESEARCHING,
        )
        s.add_all([old, fresh])
        s.flush()
        old.updated_at = datetime.now(UTC) - timedelta(hours=5)
        old_id, fresh_id = old.id, fresh.id
    assert runs.sweep_orphans() == 1
    with session_scope() as s:
        assert s.get(Job, old_id).status == JobStatus.FAILED
        assert "Interrupted" in s.get(Job, old_id).error
        assert s.get(Job, fresh_id).status == JobStatus.RESEARCHING  # may still be driven elsewhere


def test_a_lead_dropped_on_revalidation_is_not_counted_as_a_lead(clean_db, client):
    job_id = _seed(n_signal=1, n_icp=0, n_no_contact=0, n_rejected=0)
    with session_scope() as s:
        s.query(JobLead).filter(JobLead.job_id == job_id).update(
            {"status": LeadStatus.DROPPED_ON_REVALIDATION}
        )
    from app.config.database import SessionLocal

    with SessionLocal() as session:
        counts = q.lead_counts_for_jobs(session, [job_id])[job_id]
    assert (counts.qualified, counts.no_contact, counts.rejected) == (0, 0, 1)
    assert "No leads passed every check" in client.get(f"/jobs/{job_id}").text


def test_a_token_can_be_entered_on_the_sign_in_page(clean_db):
    settings = get_settings()
    settings.web_auth_token = "s3cret"
    try:
        guarded = TestClient(create_app())
        assert "Access key" in guarded.get("/").text
        assert guarded.post("/signin", data={"token": "nope", "next": "/"}).status_code == 401
        ok = guarded.post(
            "/signin", data={"token": "s3cret", "next": "//evil.example"}, follow_redirects=False
        )
        assert ok.status_code == 303
        assert ok.headers["location"] == "/"
        assert guarded.get("/").status_code == 200
    finally:
        settings.web_auth_token = ""


def test_a_generic_plan_can_be_drafted_again(clean_db, client, monkeypatch):
    started = []
    monkeypatch.setattr(runs, "start_draft", started.append)
    job_id = _parked_job()
    client.get(f"/new/{job_id}")
    res = client.post(
        f"/new/{job_id}/redraft", data={"csrf": client.cookies.get("lp_csrf")}, follow_redirects=False
    )
    assert res.status_code == 303
    assert started == [job_id]
    with session_scope() as s:
        assert s.get(Job, job_id).status == JobStatus.RESEARCHING_CLIENT
    finished = _seed(prefix="z")
    client.post(f"/new/{finished}/redraft", data={"csrf": client.cookies.get("lp_csrf")})
    assert started == [job_id]  # a finished run is never re-drafted


def test_a_draft_interrupted_by_a_restart_is_resumed(clean_db, monkeypatch):
    started = []
    monkeypatch.setattr(runs, "start_draft", started.append)
    with session_scope() as s:
        job = Job(
            client_key="k",
            client_name="K",
            ask="a",
            target_countries=["GB"],
            requested_count=5,
            cost_cap_inr=10,
            status=JobStatus.RESEARCHING_CLIENT,
        )
        s.add(job)
        s.flush()
        job_id = job.id
    with TestClient(create_app()):  # runs the startup hook
        pass
    assert started == [job_id]


def test_sign_in_is_throttled_after_repeated_wrong_keys(clean_db, monkeypatch):
    from app.middlewares import auth as web_main

    monkeypatch.setattr(web_main, "_signin_failures", {})
    settings = get_settings()
    settings.web_auth_token = "s3cret"
    try:
        guarded = TestClient(create_app())
        for _ in range(web_main.SIGNIN_MAX_FAILURES):
            assert guarded.post("/signin", data={"token": "guess"}).status_code == 401
        locked = guarded.post("/signin", data={"token": "s3cret"})
        assert locked.status_code == 429  # even the right key waits out the window
        bounced = web_main.local_path("/\\evil.example")
        assert bounced == "/"
    finally:
        settings.web_auth_token = ""


def test_intake_refuses_a_cap_above_the_ceiling(clean_db, client):
    client.get("/new")
    res = client.post(
        "/new",
        data={
            "csrf": client.cookies.get("lp_csrf"),
            "website": "acme.co.uk",
            "ask": "UK 3PLs hiring warehouse staff",
            "count": 5,
            "country": "GB",
            "cost_cap_inr": 999999,
        },
    )
    assert res.status_code == 400
    assert "Between ₹1 and ₹5,000" in res.text


def test_a_plan_awaiting_approval_is_not_counted_as_running(clean_db, client):
    job_id = _parked_job()
    assert client.get(f"/jobs/{job_id}/progress").json()["running"] is False
    with session_scope() as s:
        assert q.totals(s).running == 0


def test_a_person_can_rate_a_lead_and_the_run_page_counts_it(clean_db, client):
    _seed(n_signal=1, n_icp=0, n_no_contact=0, n_rejected=0)
    with session_scope() as s:
        lead = s.query(JobLead).first()
        lead_id, job_id = lead.id, lead.job_id
    client.get(f"/leads/{lead_id}")
    token = client.cookies.get("lp_csrf")
    res = client.post(
        f"/leads/{lead_id}/feedback",
        data={"csrf": token, "verdict": "wrong_intent", "note": "hiring in-house"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "You said: <b>Wrong intent</b>" in client.get(f"/leads/{lead_id}").text
    client.post(f"/leads/{lead_id}/feedback", data={"csrf": token, "verdict": "good"})  # changing your mind
    from app.repositories import feedback_repository

    with session_scope() as s:
        assert feedback_repository.summary_for_job(s, job_id) == {"good": 1}
        [row] = feedback_repository.export_rows(s)
    assert row["verdict"] == "good"
    assert row["signals"][0]["quote"]
    assert "<b>1</b> good" in client.get(f"/jobs/{job_id}").text
    bad = client.post(f"/leads/{lead_id}/feedback", data={"csrf": token, "verdict": "great"})
    assert bad.status_code == 400
