"""Signals-only output mode (company + dated signal, no people) and worldwide runs. Fully offline."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import openpyxl
import pytest
import respx
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config.constants import COUNTRIES, WORLDWIDE
from app.config.database import session_scope
from app.models import Contact, Delivery, Job, JobLead
from app.models.enums import JobStatus, LeadStatus, OutputMode
from app.providers.web.page_fetcher import Fetcher, PageCache
from app.services import run_manager_service as runs
from app.services.discovery_service import GL
from app.services.export_service import SIGNALS_ONLY_COLUMNS, write_outputs
from app.services.intake_service import (
    DraftedSource,
    PlanOut,
    ReviewedPlanForm,
    build_reviewed_spec,
    fallback_plan,
    parse_output_mode,
    plan_from_job,
    plan_to_json,
    planned_sources,
    stored_output_mode,
    validate_intake_form,
)
from app.services.run_service import Deps, run_job
from tests.test_runner_e2e import TODAY, FakeLLM, FakeRegistry, FakeSerper, _all_rows, _mock_sites, _spec


def _signals_spec(**update):
    return _spec().model_copy(
        update={"output_mode": OutputMode.SIGNALS_ONLY, "personalized_email": False} | update
    )


class ExplodingFinder:
    """A signals-only run must never look anyone up: any call is a failed test, not a paid lookup."""

    name = "exploding"

    async def find(self, first, last, domain):
        raise AssertionError("email finder called in a signals-only run")

    async def validate(self, emails):
        raise AssertionError("email validator called in a signals-only run")


class NoOfficersRegistry(FakeRegistry):
    async def decision_officers(self, number):
        raise AssertionError("officers looked up in a signals-only run")


# --- the spec contract ------------------------------------------------------------------------------


def test_contacts_stays_the_default_so_existing_jobs_are_unchanged():
    assert _spec().output_mode == OutputMode.CONTACTS
    assert _spec().wants_contacts


@pytest.mark.parametrize("field", ["personalized_email", "include_icp_only"])
def test_signals_only_refuses_options_that_need_people_or_no_signal(field):
    data = _spec().model_dump() | {"output_mode": "signals_only", "personalized_email": False, field: True}
    with pytest.raises(ValidationError, match="signals_only"):
        type(_spec()).model_validate(data)


# --- the run ---------------------------------------------------------------------------------------


@respx.mock
def test_signals_only_run_delivers_companies_with_signals_and_stores_no_people(tmp_path: Path, clean_db):
    """bravo's only finder result was a catch-all email: in contacts mode it lands on the no-contact sheet.
    Signals-only has no such sheet: bravo is a full lead, as is acme. charlie (2024 signal) is still out."""
    _mock_sites()
    deps = Deps(
        FakeSerper(),
        FakeLLM(),
        Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
        [ExplodingFinder()],
        NoOfficersRegistry(),
    )
    summary = asyncio.run(run_job(_signals_spec(), tmp_path / "out", deps=deps, today=TODAY))

    assert summary.status == JobStatus.DONE, summary.rejected
    assert (summary.qualified, summary.no_contact, summary.icp_only) == (2, 0, 0)
    assert any("dated within 90 days" in k for k in summary.rejected), summary.rejected
    assert set(summary.outputs) == {"leads_csv", "xlsx"}  # no no-contact file

    wb = openpyxl.load_workbook(summary.outputs["xlsx"])
    assert wb.sheetnames == ["Leads", "Methodology"]
    header = next(wb["Leads"].values)
    assert list(header) == SIGNALS_ONLY_COLUMNS
    rows = _all_rows(wb, "Leads")
    assert {r["company_domain"] for r in rows} == {"acme.co.uk", "bravo.co.uk"}
    assert [r["rank"] for r in rows] == [1, 2]
    acme = next(r for r in rows if r["company_domain"] == "acme.co.uk")
    assert acme["signal_1_date"] == "2026-10-20"
    assert acme["signal_1_source_url"].startswith("https://acme.co.uk/news/")
    assert acme["country"] == "GB"
    sheet_text = str(list(wb["Leads"].values))
    for personal in ("Jane", "Roe", "Tom Fox", "@acme.co.uk", "@bravo.co.uk"):
        assert personal not in sheet_text
    method = " ".join(str(v[0]) for v in wb["Methodology"].values)
    assert "Signals only" in method
    assert "Emails: only addresses" not in method

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Contact)) == 0  # no personal data stored
        statuses = s.scalars(select(JobLead.status).where(JobLead.status != LeadStatus.REJECTED)).all()
        assert sorted(statuses) == [LeadStatus.QUALIFIED, LeadStatus.QUALIFIED]
        assert s.scalar(select(func.count()).select_from(Delivery)) == 2


@respx.mock
def test_signals_only_never_repeats_a_delivered_company(tmp_path: Path, clean_db):
    _mock_sites()

    def deps():
        return Deps(
            FakeSerper(),
            FakeLLM(),
            Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
            [],
            FakeRegistry(),
        )

    asyncio.run(run_job(_signals_spec(), tmp_path / "out", deps=deps(), today=TODAY))
    again = asyncio.run(run_job(_signals_spec(), tmp_path / "out2", deps=deps(), today=TODAY))
    assert again.qualified == 0


# --- worldwide -------------------------------------------------------------------------------------


def test_worldwide_is_a_choosable_market_with_no_place_words_in_queries():
    assert COUNTRIES[WORLDWIDE] == "Worldwide"
    assert GL[WORLDWIDE] == "us"  # Google has no worldwide setting; stated, not a silent 'uk' fallback
    assert validate_intake_form("acme.com", "agencies needing developers", 5, "ww", 60, 50) == {}
    plan = fallback_plan("digital agencies", 5, WORLDWIDE, "acme.com", "Acme", "test")
    queries = [q for s in plan.signals for q in s["queries"]]
    assert queries
    assert not any("Worldwide" in q or q != q.strip() for q in queries)


def test_worldwide_drops_job_board_location_filters():
    board = DraftedSource(
        category="agency", search_queries=["q"], role_keywords=["developer"], locations=["UK"]
    )
    out = PlanOut.model_construct(
        job_board_source=board, job_ads_source=None, contract_awards_source=None, browse_sources=[]
    )
    assert planned_sources(out, WORLDWIDE)[0]["locations"] == []
    assert planned_sources(out, "GB")[0]["locations"] == ["UK"]


def test_worldwide_export_claims_no_country_per_company(tmp_path: Path):
    spec = _signals_spec(countries=[WORLDWIDE])
    paths = write_outputs([], spec, TODAY, tmp_path, job_id=1)
    wb = openpyxl.load_workbook(paths["xlsx"])
    assert list(next(wb["Leads"].values)) == SIGNALS_ONLY_COLUMNS
    method = " ".join(str(v[0]) for v in wb["Methodology"].values)
    assert "Worldwide run" in method


# --- the console path: intake -> stored plan -> reviewed spec ---------------------------------------


def test_output_mode_values_are_parsed_strictly():
    assert parse_output_mode("signals_only") == OutputMode.SIGNALS_ONLY
    assert parse_output_mode("") == OutputMode.CONTACTS
    assert parse_output_mode("everything") is None
    errors = validate_intake_form("acme.com", "agencies needing developers", 5, "GB", 60, 50, "", "bogus")
    assert "output_mode" in errors


def test_jobs_from_before_the_mode_existed_read_as_contacts():
    assert stored_output_mode(SimpleNamespace(icp={"intake": {}})) == OutputMode.CONTACTS
    assert stored_output_mode(SimpleNamespace(icp=None)) == OutputMode.CONTACTS
    assert (
        stored_output_mode(SimpleNamespace(id=9, icp={"intake": {"output_mode": "junk"}}))
        == OutputMode.CONTACTS
    )


def test_plan_round_trips_the_mode_and_to_spec_does_not_mutate_the_plan():
    plan = fallback_plan("digital agencies", 5, "GB", "acme.com", "Acme", "test")
    plan.output_mode, plan.personalized_email, plan.include_icp_only = OutputMode.SIGNALS_ONLY, True, True
    spec = plan.to_spec("acme-com")
    assert spec.output_mode == OutputMode.SIGNALS_ONLY
    assert (spec.personalized_email, spec.include_icp_only, spec.sender) == (False, False, None)
    # The operator's ticks are kept on the plan, so switching back to contacts restores them.
    assert plan.personalized_email
    assert plan.include_icp_only
    job = SimpleNamespace(
        ask=plan.ask,
        requested_count=5,
        target_countries=["GB"],
        client_name="Acme",
        client_website="acme.com",
        icp=plan_to_json(plan),
        freshness_days=90,
        personalized_email=True,
        cost_cap_inr=50.0,
    )
    assert plan_from_job(job).output_mode == OutputMode.SIGNALS_ONLY


def _review_job(mode: OutputMode):
    plan = fallback_plan("digital agencies", 5, WORLDWIDE, "acme.com", "Acme", "test")
    plan.output_mode = mode
    return SimpleNamespace(
        ask=plan.ask,
        target_countries=[WORLDWIDE],
        client_name="Acme",
        client_website="acme.com",
        client_key="acme-com",
        cost_cap_inr=50.0,
        icp=plan_to_json(plan),
    )


def _review_form(mode: OutputMode, buyer_titles: str = "") -> ReviewedPlanForm:
    return ReviewedPlanForm(
        ask="digital agencies needing white-label developers",
        count=5,
        freshness_days=60,
        cost_cap_inr=50,
        personalized_email=True,  # ticked, but meaningless without contacts
        output_mode=mode,
        buyer_titles=buyer_titles,
        signal_type=["hiring"],
        signal_queries=['"white label" developer agency hiring'],
    )


def test_signals_only_review_needs_no_buyer_titles_and_runs_worldwide():
    spec = build_reviewed_spec(_review_job(OutputMode.SIGNALS_ONLY), _review_form(OutputMode.SIGNALS_ONLY))
    assert spec.output_mode == OutputMode.SIGNALS_ONLY
    assert spec.countries == [WORLDWIDE]
    assert spec.personalized_email is False


def test_contacts_review_still_needs_buyer_titles():
    from app.exceptions.custom_exceptions import BadRequestError

    with pytest.raises(BadRequestError, match="buyer title"):
        build_reviewed_spec(_review_job(OutputMode.CONTACTS), _review_form(OutputMode.CONTACTS))


def test_intake_stores_the_chosen_mode_on_the_job(clean_db):
    job_id = runs.create_draft_job(
        "acme.com",
        "agencies needing developers",
        5,
        WORLDWIDE,
        "Acme",
        60,
        False,
        False,
        50.0,
        OutputMode.SIGNALS_ONLY,
    )
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert stored_output_mode(job) == OutputMode.SIGNALS_ONLY
        assert job.target_countries == [WORLDWIDE]


def test_console_form_starts_a_worldwide_signals_only_run(clean_db, monkeypatch):
    """Through the real routes: intake stores the mode, the review page offers it, start sends it on."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    monkeypatch.setattr(runs, "start_draft", lambda job_id: None)
    launched: list = []
    monkeypatch.setattr(runs, "start_run", lambda job_id, spec: launched.append(spec))
    client.get("/new")
    token = client.cookies.get("lp_csrf")
    res = client.post(
        "/new",
        data={
            "website": "acme.com",
            "ask": "agencies needing white-label developers",
            "count": 5,
            "country": "WW",
            "output_mode": "signals_only",
            "cost_cap_inr": 50,
            "csrf": token,
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    job_id = int(res.headers["location"].rsplit("/", 1)[1])
    with session_scope() as s:  # park it at review, as the background drafting would
        job = s.get(Job, job_id)
        plan = fallback_plan(job.ask, 5, WORLDWIDE, "acme.com", "Acme", "test")
        plan.output_mode = stored_output_mode(job)
        job.icp, job.status = plan_to_json(plan), JobStatus.AWAITING_ICP_REVIEW
    page = client.get(f"/new/{job_id}").text
    assert '<option value="signals_only" selected' in page
    bad = client.post(f"/new/{job_id}/start", data={"csrf": token, "output_mode": "everything"})
    assert bad.status_code == 400  # an unknown mode is refused, never silently read as contacts
    assert launched == []
    res = client.post(
        f"/new/{job_id}/start",
        data={
            "csrf": token,
            "output_mode": "signals_only",
            "count": 5,
            "freshness_days": 60,
            "cost_cap_inr": 50,
            "signal_type": ["hiring"],
            "signal_queries": ['"white label" developer agency hiring'],
        },
        follow_redirects=False,
    )
    assert res.status_code == 303, res.text[:300]
    assert launched[0].output_mode == OutputMode.SIGNALS_ONLY
    assert launched[0].countries == [WORLDWIDE]
