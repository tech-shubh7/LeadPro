"""Regression tests for the 2026-11 audit fixes (one test per defect)."""

import asyncio
import json
from datetime import date

import httpx
import pytest
import respx

from app.config.database import session_scope
from app.exceptions.custom_exceptions import CostCapExceededError, LLMRefusalError
from app.models import Contact, Delivery, Job, JobLead, Suppression
from app.models.enums import JobStatus, LeadStatus
from app.providers.llm.manual_provider import ManualLLMProvider
from app.providers.web.page_fetcher import Fetcher, PageCache
from app.repositories import lead_repository as store
from app.services.draft_service import DraftOut, draft_is_grounded
from app.services.extraction_service import ExtractOut, QuotedSignal, verified_extraction
from app.services.research_service import ResearchBundle
from app.services.scoring_service import ScoreOut
from app.services.source_service import collect_sources
from app.utils.cost_tracker import CostTracker

TODAY = date(2026, 11, 11)
FILLER = "Acme Goods makes kitchen equipment for restaurants across the north of England. " * 8


# --- cost cap -----------------------------------------------------------------------------------


def test_calls_in_flight_count_against_the_cap():
    """Eight workers each passing the check before any records must not overshoot the cap together."""
    t = CostTracker(cap_inr=10)

    async def paid_call():
        with t.reserve(4):
            await asyncio.sleep(0.01)
            t.record("x", "y", 1, 4)

    async def main():
        return await asyncio.gather(*(paid_call() for _ in range(8)), return_exceptions=True)

    results = asyncio.run(main())
    assert sum(isinstance(r, CostCapExceededError) for r in results) == 6
    assert t.spent_inr == 8 <= t.cap_inr
    assert t.reserved_inr == 0


def test_a_failed_call_releases_its_reservation():
    t = CostTracker(cap_inr=5)
    with pytest.raises(RuntimeError), t.reserve(4):
        raise RuntimeError("provider down")
    t.ensure_budget(4)  # the failed call's estimate is no longer held


# --- store ----------------------------------------------------------------------------------------


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


def test_rejecting_a_company_the_job_already_holds_does_not_fail(clean_db):
    """A second domain resolving to an already-saved company used to raise on the (job, company) key
    inside the worker's error handler, which failed the whole job."""
    with session_scope() as s:
        job = _job(s)
        company = store.upsert_company(s, "acme.co.uk", "GB")
        s.add(
            JobLead(
                job_id=job.id,
                company_id=company.id,
                status=LeadStatus.QUALIFIED,
                fit_score=30,
                intent_score=30,
                timing_score=10,
                total_score=70,
                lead_tier="signal",
            )
        )
        s.flush()
        store.save_rejection(s, job, "acme.co.uk", "GB", "duplicate company: companies_house 01234567")
    with session_scope() as s:
        leads = s.query(JobLead).all()
        assert [lead.status for lead in leads] == [LeadStatus.QUALIFIED]  # the lead stands, not overwritten


def test_erasing_two_people_at_one_company_keeps_one_delivery_record(clean_db):
    """contact_id is SET NULL on delete and (client, company, NULL) is unique: erasing the second person at
    a company (or one at a company with a company-only delivery) used to violate it, so erasure failed."""
    with session_scope() as s:
        job = _job(s)
        company = store.upsert_company(s, "acme.co.uk", "GB")
        people = []
        for first, email in (("Ann", "ann@acme.co.uk"), ("Bob", "bob@acme.co.uk")):
            c = Contact(
                company_id=company.id,
                first_name=first,
                last_name="Lee",
                job_title="Director",
                person_source_url="https://acme.co.uk/team",
                person_source_quote="x",
                email=email,
                email_verdict="valid",
                email_verified_at=TODAY,
            )
            s.add(c)
            s.flush()
            people.append(c)
            s.add(Delivery(client_key="k", company_id=company.id, contact_id=c.id, job_id=job.id))
        s.flush()
        company_id = company.id
    for email in ("ann@acme.co.uk", "bob@acme.co.uk"):
        with session_scope() as s:
            assert store.erase_person_by_email(s, email, "erasure request") == 1
    with session_scope() as s:
        assert s.query(Contact).count() == 0
        rows = s.query(Delivery).all()
        assert [(r.company_id, r.contact_id) for r in rows] == [
            (company_id, None)
        ]  # still never re-delivered
        assert s.query(Suppression).count() == 2


# --- drafts ---------------------------------------------------------------------------------------


def test_invented_numbers_cannot_hide_inside_longer_numbers():
    listing = {"E1": '[E1] (funding, dated 2026-10-01) Raised £25m to open 50 stores | quote: "raised £25m"'}
    real = DraftOut(
        subject="Your 50 stores", body="Saw the £25m raise to open 50 stores.", cited_evidence_ids=["E1"]
    )
    invented = DraftOut(
        subject="Hi", body="Opening 5 stores a month must be busy.", cited_evidence_ids=["E1"]
    )
    assert draft_is_grounded(real, listing)
    assert not draft_is_grounded(invented, listing)  # '5' used to pass because '25' and '50' contain it


# --- manual LLM mode ------------------------------------------------------------------------------


def test_a_cached_answer_that_no_longer_validates_is_listed_again(tmp_path):
    """A response file with no task file (answered earlier) that fails validation must re-create the task,
    or the operator never sees it and the run waits out the timeout."""
    llm = ManualLLMProvider(CostTracker(cap_inr=1), task_dir=tmp_path, poll_s=0.01, timeout_s=0.3)

    async def main():
        first = asyncio.create_task(llm.parse("m", "score it", "<evidence>", ScoreOut))
        while not list(tmp_path.glob("*.task.json")):  # noqa: ASYNC110 - waiting on a file the provider writes
            await asyncio.sleep(0.01)
        task_id = json.loads(next(tmp_path.glob("*.task.json")).read_text())["id"]
        first.cancel()
        (tmp_path / f"{task_id}.task.json").unlink()
        (tmp_path / f"{task_id}.response.json").write_text('{"fit_score": "lots"}')  # stale/invalid cache
        with pytest.raises(LLMRefusalError):  # the stale answer is rejected, then the wait times out
            await llm.parse("m", "score it", "<evidence>", ScoreOut)
        return task_id

    task_id = asyncio.run(main())
    assert (tmp_path / f"{task_id}.task.json").exists()


# --- fetcher --------------------------------------------------------------------------------------


@respx.mock
def test_non_html_and_oversized_pages_are_not_read(tmp_path):
    respx.get("https://acme.co.uk/robots.txt").respond(404)
    respx.get("https://acme.co.uk/brochure.pdf").respond(
        200, content=b"%PDF" * 10, headers={"content-type": "application/pdf"}
    )
    respx.get("https://acme.co.uk/huge").respond(200, html="<p>x</p>", headers={"content-length": str(10**8)})
    fetcher = Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path, ttl=None))

    async def main():
        pdf = await fetcher.fetch("https://acme.co.uk/brochure.pdf")
        huge = await fetcher.fetch("https://acme.co.uk/huge")
        await fetcher.aclose()
        return pdf, huge

    pdf, huge = asyncio.run(main())
    assert pdf.html == ""
    assert pdf.text == ""
    assert huge.blocked_reason == "page too large"
    assert not huge.ok


# --- extraction -----------------------------------------------------------------------------------


def test_a_signal_cited_with_a_url_variant_keeps_its_page_date():
    """The quote check accepts 'https://www.acme.co.uk/news/2026/10/x/' for the fetched
    'https://acme.co.uk/news/2026/10/x'; dating used the raw URL and lost the date, dropping the signal."""
    from app.providers.web.page_fetcher import FetchResult

    url = "https://acme.co.uk/news/2026/10/opening"
    html = (
        '<html><head><meta property="article:published_time" content="2026-10-20"></head><body><p>'
        f"Acme Goods opened a new warehouse in Leeds this month. {FILLER}</p></body></html>"
    )
    page = FetchResult(
        url, url, 200, html, f"Acme Goods opened a new warehouse in Leeds this month. {FILLER}", "2026-11-11"
    )
    bundle = ResearchBundle("acme.co.uk", page, pages={url: page})
    out = ExtractOut(
        sources_match_target=True,
        company_name="Acme",
        company_description="d",
        is_recruitment_or_staffing_firm=False,
        facts=[],
        people=[],
        signals=[
            QuotedSignal(
                claim="opened a warehouse",
                source_url="https://www.acme.co.uk/news/2026/10/opening/",
                quote="Acme Goods opened a new warehouse in Leeds",
                signal_type="expansion",
            )
        ],
    )
    ext = verified_extraction(out, bundle, {url: page.text}, TODAY)
    assert len(ext.signals) == 1
    assert ext.signals[0].source_url == url
    assert ext.signals[0].effective_date == date(2026, 10, 20)


# --- structured sources ---------------------------------------------------------------------------


def test_a_failing_public_dataset_skips_that_source_only():
    from app.schemas.job_spec_schema import JobSpec

    class DownContracts:
        async def awards(self, *a, **k):
            raise httpx.ConnectError("contracts finder down")

    spec = JobSpec.model_validate(
        {
            "client": {"key": "k", "name": "n"},
            "ask": "a",
            "count": 1,
            "icp": {},
            "playbook": {"signals": [], "buyer_titles": ["ceo"]},
            "sources": [{"type": "contracts_awards", "keywords": ["website"]}],
        }
    )
    found, used = asyncio.run(
        collect_sources(spec, None, {"ats": object(), "contracts": DownContracts()}, "uk", TODAY, 5)
    )
    assert found == []
    assert used == 0


# --- discovery ------------------------------------------------------------------------------------


def test_candidates_found_before_the_cap_are_handed_back():
    from app.schemas.job_spec_schema import JobSpec
    from app.services.discovery_service import discover

    class CappedSerper:
        async def query(self, *a, **k):
            raise CostCapExceededError("cap")

        async def resolve_redirect(self, link):
            return link

    spec = JobSpec.model_validate(
        {
            "client": {"key": "k", "name": "n"},
            "ask": "a",
            "count": 3,
            "icp": {},
            "playbook": {
                "signals": [{"type": "t", "description": "d", "queries": ["q"]}],
                "buyer_titles": ["ceo"],
            },
            "seeds": [{"domain": "acme.co.uk", "name": "Acme"}],
        }
    )
    with pytest.raises(CostCapExceededError) as info:
        asyncio.run(discover(spec, CappedSerper(), None, set(), 5, today=TODAY))
    assert [c.domain for c in info.value.candidates] == ["acme.co.uk"]


def test_named_targets_still_get_resolved_when_rounds_use_the_budget():
    """Job 12: rounds spent every search, so each company triage named was silently dropped unresolved."""
    from app.providers.search.serper_client import SearchHit
    from app.schemas.job_spec_schema import JobSpec
    from app.services.discovery_service import HitTriage, TriageOut, discover

    calls: list[str] = []

    class Serper:
        async def query(self, q, kind="search", **k):
            calls.append(q)
            if "official website" in q:
                return [
                    SearchHit(
                        "Brightwave Robotics — home",
                        "https://brightwave.co.uk/",
                        "Robotics firm",
                        None,
                        "search",
                        q,
                    )
                ]
            return [
                SearchHit(
                    "Brightwave Robotics invites proposals for a new website",
                    "https://news.example/x",
                    "Brightwave Robotics is seeking an agency",
                    None,
                    "news",
                    q,
                )
            ]

        async def resolve_redirect(self, link):
            return link

    class LLM:
        extract_model = judge_model = "m"

        async def parse(self, model, instructions, data, schema, **k):
            n = sum(1 for line in data.splitlines() if line.startswith("["))
            return TriageOut(
                items=[
                    HitTriage(
                        hit_index=i,
                        company_name="Brightwave Robotics",
                        entity_type="target_company",
                        link_is_company_own_site=False,
                        icp_fit=8,
                        category="robotics firm",
                    )
                    for i in range(n)
                ]
            )

    spec = JobSpec.model_validate(
        {
            "client": {"key": "k", "name": "n"},
            "ask": "a",
            "count": 5,
            "icp": {},
            "max_search_queries": 6,
            "playbook": {
                "signals": [{"type": "t", "description": "d", "queries": [f"q{i}" for i in range(6)]}],
                "buyer_titles": ["ceo"],
            },
        }
    )
    found = asyncio.run(discover(spec, Serper(), LLM(), set(), 5, today=TODAY))
    assert [c.domain for c in found] == ["brightwave.co.uk"]
    assert len(calls) <= 6  # still inside the budget


def test_job_boards_never_pass_for_a_company_website():
    """Job 13: 'Temporal' resolved to ziprecruiter.com, 'Ramp' to glassdoor.be."""
    from app.providers.search.serper_client import SearchHit
    from app.services.discovery_service import is_third_party, resolve_domain

    for d in ("glassdoor.be", "uk.indeed.com", "ziprecruiter.com", "jobs.ashbyhq.com", "remoteok.com"):
        assert is_third_party(d), d
    assert not is_third_party("greenhousecafe.co.uk")  # whole labels only
    for d in ("gamesjobs.co.uk", "cwjobs.co.uk", "imdb.com"):  # job 16 researched these as buyers
        assert is_third_party(d), d
    assert not is_third_party("acmejobsolutions.co.uk")

    class Serper:
        async def query(self, q, **k):
            return [
                SearchHit(
                    "Temporal jobs - Apply now",
                    "https://jobs.example-board.com/temporal",
                    "",
                    None,
                    "search",
                    q,
                ),
                SearchHit("Temporal | Durable execution", "https://temporal.io/", "", None, "search", q),
            ]

    assert asyncio.run(resolve_domain(Serper(), "Temporal", "uk")) == "temporal.io"


def test_exported_cells_cannot_run_as_formulas():
    """Company names, quotes and drafts come from web pages: a cell must never become a formula."""
    from app.services.export_service import safe_cell as cell

    assert cell('=HYPERLINK("http://evil","x")') == '\'=HYPERLINK("http://evil","x")'
    for bad in ("+cmd|' /C calc'!A0", "-2+3", "@SUM(A1)", "\t=1"):
        assert cell(bad).startswith("'")
    assert cell("Acme Ltd") == "Acme Ltd"
    assert cell(42) == 42
    assert cell("+441132345678") == "+441132345678"


def test_old_cached_pages_are_deleted(tmp_path):
    """Cached team pages name people: they must not be kept forever."""
    import os
    import time as _time
    from datetime import timedelta

    cache = PageCache(tmp_path, None)
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old.write_text("{}")
    new.write_text("{}")
    os.utime(old, (_time.time() - 40 * 86400,) * 2)
    assert cache.prune(timedelta(days=30)) == 1
    assert not old.exists()
    assert new.exists()


def test_source_candidates_outside_the_icp_are_not_researched():
    """Job 18: asked for digital agencies, the job boards returned fintech and SaaS companies."""
    from app.providers.search.serper_client import SearchHit
    from app.schemas.job_spec_schema import JobSpec
    from app.services.discovery_service import Candidate, HitTriage, TriageOut, icp_gate

    def cand(name):
        return Candidate(
            domain="",
            name_hint=name,
            seed_hits=[
                SearchHit(
                    f"{name} job posting: Developer",
                    f"https://jobs.lever.co/{name.lower()}/1",
                    "",
                    None,
                    "ats",
                    "q",
                )
            ],
        )

    class LLM:
        extract_model = judge_model = "m"

        async def parse(self, model, instructions, data, schema, **k):
            return TriageOut(
                items=[
                    HitTriage(
                        hit_index=0,
                        company_name="Pixel Agency",
                        entity_type="target_company",
                        link_is_company_own_site=False,
                        icp_fit=8,
                    ),
                    HitTriage(
                        hit_index=1,
                        company_name="Sardine",
                        entity_type="target_company",
                        link_is_company_own_site=False,
                        icp_fit=1,
                    ),
                ]
            )

    spec = JobSpec.model_validate(
        {
            "client": {"key": "k", "name": "n"},
            "ask": "UK digital agencies",
            "count": 3,
            "icp": {},
            "playbook": {"signals": [], "buyer_titles": ["md"]},
        }
    )
    kept = asyncio.run(icp_gate(spec, LLM(), [cand("Pixel Agency"), cand("Sardine")]))
    assert [c.name_hint for c in kept] == ["Pixel Agency"]
