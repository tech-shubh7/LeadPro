"""Every company records where it was found, rejections keep the evidence they saw, and a report shows
which source produces qualified (and approved) leads. Without this, jobs 26-28 could not say whether the
shortfall was source coverage, the playbook or qualification. Fully offline."""

import asyncio
from pathlib import Path

import httpx
import respx
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli import cli
from app.config.database import session_scope
from app.models import Company, Job, JobLead, LeadFeedback
from app.models.enums import LeadStatus
from app.providers.search.serper_client import SearchHit
from app.providers.web.page_fetcher import Fetcher, PageCache
from app.repositories import console_repository as repo
from app.repositories import lead_repository as store
from app.services.discovery_service import (
    CHANNEL_ATS,
    CHANNEL_BROWSE,
    CHANNEL_CONTRACTS,
    CHANNEL_LINKEDIN,
    CHANNEL_REED,
    CHANNEL_SEED,
    CHANNEL_UNKNOWN,
    CHANNEL_WEB_NEWS,
    CHANNEL_WEB_SEARCH,
    Candidate,
    Origin,
    candidate_origin,
)
from app.services.extraction_service import ExtractOut
from tests import test_runner_e2e as e2e


def _hit(query: str, kind: str = "search") -> SearchHit:
    return SearchHit("t", "https://x.example/a", "s", None, kind, query)


def test_the_channel_is_read_from_the_hit_that_found_the_company():
    cases = [
        (_hit("ats:lever:acme", "ats"), CHANNEL_ATS),
        (_hit("linkedin:4490400834", "job_ad"), CHANNEL_LINKEDIN),
        (_hit("reed:123", "job_ad"), CHANNEL_REED),
        (_hit("contracts_finder:award", "notice"), CHANNEL_CONTRACTS),
        (_hit("browse:https://cohort.example", "search"), CHANNEL_BROWSE),
        (_hit('agency "contract wordpress developer"'), CHANNEL_WEB_SEARCH),
        (_hit("agency hires developers", "news"), CHANNEL_WEB_NEWS),
    ]
    for hit, channel in cases:
        later = _hit("linkedin:999", "job_ad")  # a hit merged in later does not change who found it
        origin = candidate_origin(Candidate("a.com", "A", seed_hits=[hit, later]))
        assert origin == Origin(channel, hit.query)
    assert candidate_origin(Candidate("a.com", "A", seeded=True)).channel == CHANNEL_SEED
    assert candidate_origin(Candidate("a.com", "A")).channel == CHANNEL_UNKNOWN


def _run(tmp_path: Path, llm) -> int:
    with respx.mock:
        e2e._mock_sites()
        deps = e2e.Deps(
            e2e.FakeSerper(),
            llm,
            Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
            [e2e.FakeFinder()],
            e2e.FakeRegistry(),
        )
        return asyncio.run(e2e.run_job(e2e._spec(), tmp_path / "out", deps=deps, today=e2e.TODAY)).job_id


def _rows(job_id: int) -> dict[str, JobLead]:
    with session_scope() as s:
        rows = s.execute(
            select(Company.domain, JobLead)
            .join(JobLead, JobLead.company_id == Company.id)
            .where(JobLead.job_id == job_id)
        ).all()
        for _, lead in rows:
            s.expunge(lead)
        return dict(rows)


def _no_mx(monkeypatch):
    async def mx(domain):
        return True

    monkeypatch.setattr(e2e.contacts, "has_mx", mx)


def test_every_researched_company_records_its_source(tmp_path, clean_db, monkeypatch):
    _no_mx(monkeypatch)
    rows = _rows(_run(tmp_path, e2e.FakeLLM()))
    assert set(rows) == set(e2e.SITES)
    for lead in rows.values():  # FakeSerper's web results found all three
        assert lead.found_by == CHANNEL_WEB_SEARCH
        assert lead.found_query
    assert rows["charlie.co.uk"].reject_reason.startswith("no source dated within")
    assert rows["acme.co.uk"].status == LeadStatus.QUALIFIED


def test_a_rejection_after_reading_keeps_the_verified_evidence_it_saw(tmp_path, clean_db, monkeypatch):
    class MismatchLLM(e2e.FakeLLM):
        async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
            out = await super().parse(model, instructions, data, schema, effort, max_output_tokens)
            if schema is ExtractOut and "website is acme.co.uk" in instructions:
                out.sources_match_target = False
            return out

    _no_mx(monkeypatch)
    acme = _rows(_run(tmp_path, MismatchLLM()))["acme.co.uk"]
    assert acme.status == LeadStatus.REJECTED
    assert "different organisation" in acme.reject_reason
    assert acme.seen_evidence, "the signal it found is kept for review"
    first = acme.seen_evidence[0]
    assert first["url"] == e2e.news_url("acme.co.uk")
    assert "warehouse operatives" in first["quote"]
    assert first["date"] == "2026-10-20"
    assert first["against"] is False


def test_the_report_counts_each_stage_per_source_and_human_ratings(tmp_path, clean_db, monkeypatch):
    _no_mx(monkeypatch)
    job_id = _run(tmp_path, e2e.FakeLLM())
    with session_scope() as s:
        job = s.get(Job, job_id)
        acme = s.scalar(
            select(JobLead).join(Company).where(JobLead.job_id == job_id, Company.domain == "acme.co.uk")
        )
        s.add(LeadFeedback(job_lead_id=acme.id, verdict="good"))
        # A row from before sources were recorded, scored as a near miss.
        store.save_rejection(s, job, "old.co.uk", "GB", "score 61 below 70: thin", None)
        old = s.scalar(select(JobLead).join(Company).where(Company.domain == "old.co.uk"))
        old.fit_score, old.intent_score, old.timing_score, old.total_score = 30, 21, 10, 61
    with session_scope() as s:
        report = repo.source_performance(s, [job_id])
        everything = repo.source_performance(s)
    by = {r.channel: r for r in report.rows}
    web = by[CHANNEL_WEB_SEARCH]
    assert (web.found, web.skipped_before_model, web.qualified, web.approved) == (3, 1, 2, 1)
    assert web.scored == 2
    untracked = by[repo.UNTRACKED]
    assert (untracked.found, untracked.near_misses, untracked.qualified) == (1, 1, 0)
    assert report.rows[0].channel == CHANNEL_WEB_SEARCH  # best yield first
    assert report.qualified == 2
    assert report.approved == 1
    assert report.cost_per(0) is None
    assert {r.channel for r in everything.rows} == set(by)


def test_a_seeded_rejection_is_saved_with_its_origin_and_evidence(clean_db):
    with session_scope() as s:
        job = Job(
            client_key="c",
            client_name="C",
            ask="a",
            target_countries=["GB"],
            requested_count=1,
            freshness_days=60,
            icp={},
            cost_cap_inr=50.0,
        )
        s.add(job)
        s.flush()
        evidence = [{"url": "https://x.example/a", "quote": "q", "against": True}]
        store.save_rejection(
            s,
            job,
            "seeded.co.uk",
            "GB",
            "no buying intent",
            None,
            Origin(CHANNEL_SEED, "Seeded Ltd"),
            evidence,
        )
        row = s.scalar(select(JobLead).where(JobLead.job_id == job.id))
        assert (row.found_by, row.found_query, row.seen_evidence) == (CHANNEL_SEED, "Seeded Ltd", evidence)


def test_the_sources_command_prints_the_report(tmp_path, clean_db, monkeypatch):
    _no_mx(monkeypatch)
    job_id = _run(tmp_path, e2e.FakeLLM())
    result = CliRunner().invoke(cli, ["sources", "--job", str(job_id)])
    assert result.exit_code == 0, result.output
    assert "web_search" in result.output
    assert "per qualified lead" in result.output


def test_the_job_page_shows_where_companies_came_from(tmp_path, clean_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    _no_mx(monkeypatch)
    job_id = _run(tmp_path, e2e.FakeLLM())
    page = TestClient(app).get(f"/jobs/{job_id}?tab=rejected")
    assert page.status_code == 200
    assert "Where companies came from" in page.text
    assert "Web search" in page.text
