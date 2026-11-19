"""The measurements behind the two proofs (2026-11-19):
1. selective reading costs fewer tokens without losing genuine leads: every paid call is tagged with its step
   and company, and a research-check run reads each company the old full way too and compares;
2. which discovery sources produce good leads: spend is split by the source that found each company.
Fully offline: fake model, mocked web."""

import asyncio
from pathlib import Path
from typing import ClassVar

import httpx
import respx
from typer.testing import CliRunner

from app.cli import cli
from app.config.database import session_scope
from app.config.settings import get_settings
from app.models import Job, JobLead
from app.providers.web.page_fetcher import Fetcher, PageCache
from app.repositories import console_repository as repo
from app.services import research_check_service
from app.services.extraction_service import ExtractOut
from app.utils.cost_tracker import CostTracker, UsageLine, usage_tags
from tests import test_runner_e2e as e2e

# --- tagging -----------------------------------------------------------------------------------------


def test_every_call_is_tagged_with_its_step_and_company():
    cost = CostTracker(cap_inr=10)
    cost.record("openai", "m", 10, 0.1)
    with usage_tags(channel="linkedin_jobs", domain="a.com"), usage_tags(stage="extract"):
        cost.record("openai", "m", 5000, 0.5)
    with usage_tags(stage="score"):
        cost.record("openai", "m", 2000, 0.4)
    assert [(line.stage, line.channel, line.domain) for line in cost.lines] == [
        ("", "", ""),
        ("extract", "linkedin_jobs", "a.com"),
        ("score", "", ""),
    ]


def test_concurrent_companies_never_see_each_others_tags():
    cost = CostTracker(cap_inr=10)

    async def company(domain: str, pause: float) -> None:
        with usage_tags(domain=domain, stage="extract"):
            await asyncio.sleep(pause)
            cost.record("openai", "m", 1, 0.0)

    async def go():
        await asyncio.gather(company("slow.com", 0.02), company("fast.com", 0.0))

    asyncio.run(go())
    assert sorted(line.domain for line in cost.lines) == ["fast.com", "slow.com"]


def test_lines_saved_before_tagging_still_load():
    assert UsageLine(**{"provider": "openai", "operation": "m", "units": 1, "cost_inr": 0.1}).stage == ""


# --- research check ----------------------------------------------------------------------------------


class CountingLLM(e2e.FakeLLM):
    """Records how many characters each extraction was shown."""

    shown: list[int] = []  # noqa: RUF012 - reset per test

    async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
        if schema is ExtractOut:
            CountingLLM.shown.append(len(data))
        return await super().parse(model, instructions, data, schema, effort, max_output_tokens)


def _run(tmp_path: Path, monkeypatch, check: bool):
    async def mx(domain):
        return True

    monkeypatch.setattr(e2e.contacts, "has_mx", mx)
    monkeypatch.setattr(get_settings(), "research_check", check)
    CountingLLM.shown = []
    with respx.mock:
        e2e._mock_sites()
        deps = e2e.Deps(
            e2e.FakeSerper(),
            CountingLLM(),
            Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
            [e2e.FakeFinder()],
            e2e.FakeRegistry(),
        )
        return asyncio.run(e2e.run_job(e2e._spec(), tmp_path / "out", deps=deps, today=e2e.TODAY))


def test_a_normal_run_does_no_research_check(tmp_path, clean_db, monkeypatch):
    summary = _run(tmp_path, monkeypatch, check=False)
    assert summary.research_check == []
    assert len(CountingLLM.shown) == 2  # acme and bravo: charlie was skipped free


def test_a_check_run_compares_every_company_with_the_full_reading(tmp_path, clean_db, monkeypatch):
    summary = _run(tmp_path, monkeypatch, check=True)
    assert len(CountingLLM.shown) == 5  # 2 selective + 3 full (including the skipped one)
    rows = {r["domain"]: r for r in summary.research_check}
    assert set(rows) == set(e2e.SITES)

    charlie = rows["charlie.co.uk"]  # skipped free: the full reading confirms there was nothing fresh
    assert charlie["gated"]
    assert charlie["selective_outcome"] == research_check_service.SKIPPED
    assert charlie["full_fresh"] == []
    assert charlie["full_chars"] > 0
    assert charlie["selective_chars"] == 0

    acme = rows["acme.co.uk"]
    assert not acme["gated"]
    assert acme["selective_outcome"] == "lead"
    assert acme["full_outcome"] == "lead"
    assert acme["lost_urls"] == []
    assert [s["url"] for s in acme["full_fresh"]] == [s["url"] for s in acme["selective_fresh"]]
    assert acme["selective_chars"] <= acme["full_chars"]

    with session_scope() as s:
        job = s.get(Job, summary.job_id)
        assert job.progress["research_check"] == summary.research_check
        result = research_check_service.report(job)
    assert result["companies"] == 3
    assert result["skipped_free"] == 1
    assert result["skipped_with_fresh_signal"] == []
    assert result["pages_with_lost_signal"] == {}
    assert result["outcome_changed"] == []

    out = CliRunner().invoke(cli, ["research-check", str(summary.job_id)])
    assert out.exit_code == 0, out.output
    assert '"skipped_free": 1' in out.output


def test_the_report_counts_tokens_of_each_reading_and_flags_losses():
    class FakeJob:
        cost_lines: ClassVar[list[dict]] = [
            {"provider": "openai", "operation": "m", "units": 4000, "cost_inr": 0.1, "stage": "extract"},
            {"provider": "openai", "operation": "m", "units": 2000, "cost_inr": 0.4, "stage": "score"},
            {
                "provider": "openai",
                "operation": "m",
                "units": 9000,
                "cost_inr": 0.2,
                "stage": "check_extract",
            },
            {"provider": "openai", "operation": "m", "units": 2000, "cost_inr": 0.4, "stage": "check_score"},
            {"provider": "serper", "operation": "search", "units": 1, "cost_inr": 0.1, "stage": "discovery"},
        ]
        progress: ClassVar[dict] = {
            "research_check": [
                {
                    "domain": "a.com",
                    "gated": True,
                    "selective_chars": 0,
                    "full_chars": 30000,
                    "full_fresh": [{"url": "u1"}],
                    "selective_fresh": [],
                    "full_outcome": "lead",
                    "full_score": 72,
                    "selective_outcome": "skipped free",
                    "selective_score": None,
                    "lost_urls": ["u1"],
                },
                {
                    "domain": "b.com",
                    "gated": False,
                    "selective_chars": 12000,
                    "full_chars": 40000,
                    "full_fresh": [],
                    "selective_fresh": [],
                    "full_outcome": "no fresh signal",
                    "full_score": None,
                    "selective_outcome": "rejected: no verified buying sign",
                    "selective_score": None,
                    "lost_urls": [],
                },
            ]
        }

    result = research_check_service.report(FakeJob())
    assert (result["selective_tokens"], result["full_tokens"]) == (6000, 11000)
    assert (result["selective_chars"], result["full_chars"]) == (12000, 70000)
    assert result["skipped_with_fresh_signal"] == ["a.com"]  # would be a real loss: listed, never hidden
    assert result["pages_with_lost_signal"] == {"a.com": ["u1"]}
    assert result["outcome_changed"][0]["domain"] == "a.com"
    assert "discovery" not in result["tokens_by_stage"]  # a search is not model tokens


# --- cost per source ---------------------------------------------------------------------------------


def test_spend_is_split_by_the_source_that_found_each_company(tmp_path, clean_db, monkeypatch):
    summary = _run(tmp_path, monkeypatch, check=False)
    with session_scope() as s:
        job = s.get(Job, summary.job_id)
        job.cost_inr = 3.0
        job.cost_lines = [
            {"provider": "openai", "operation": "m", "units": 3000, "cost_inr": 1.0, "stage": "discovery"},
            {
                "provider": "openai",
                "operation": "m",
                "units": 5000,
                "cost_inr": 1.5,
                "stage": "extract",
                "channel": "web_search",
                "domain": "acme.co.uk",
            },
            {
                "provider": "serper",
                "operation": "search",
                "units": 1,
                "cost_inr": 0.3,
                "stage": "research",
                "channel": "web_search",
                "domain": "acme.co.uk",
            },
            {"provider": "openai", "operation": "m", "units": 100, "cost_inr": 0.2},  # before tagging
        ]
        report = repo.source_performance(s, [summary.job_id])
        kept = sum(
            1 for lead in s.query(JobLead).filter_by(job_id=summary.job_id) if lead.status != "rejected"
        )
    web = next(r for r in report.rows if r.channel == "web_search")
    assert web.cost_inr == 1.8
    assert web.tokens == 5000  # model tokens only; a search is not a token
    assert web.cost_per_kept == 1.8 / kept
    assert (report.shared_cost_inr, report.untagged_cost_inr, report.cost_inr) == (1.0, 0.2, 3.0)
    out = CliRunner().invoke(cli, ["sources", "--job", str(summary.job_id)])
    assert "finding candidates ₹1.00, shared" in out.output
