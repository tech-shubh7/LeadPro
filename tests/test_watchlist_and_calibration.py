"""Near misses go on a Watchlist sheet; ratings tell whether the bar is right.

Found in job 28 (2026-11-18): six agencies with a real, dated hiring sign scored 56-65 against a bar of 70 and
were thrown away with nothing kept. The Watchlist keeps them, clearly apart from leads. Whether the bar
should move is a question for the ratings, not a guess: the calibration report answers it once there are
enough. Fully offline.
"""

import asyncio
from pathlib import Path
from typing import ClassVar

import httpx
import openpyxl
import respx
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli import cli
from app.config.database import session_scope
from app.config.settings import get_settings
from app.models import Company, Delivery, Job, JobLead, LeadFeedback
from app.models.enums import LeadStatus, OutputMode
from app.providers.web.page_fetcher import Fetcher, PageCache
from app.repositories import console_repository as repo
from app.repositories import feedback_repository as fb
from app.repositories import lead_repository as store
from app.services.scoring_service import Score, ScoreOut, on_watchlist
from app.utils.template_filters import file_label
from tests import test_runner_e2e as e2e


def _score(fit: int, intent: int, timing: int) -> Score:
    return Score(fit, intent, timing, "why", "angle", "risks", [])


def test_the_watchlist_rule_matches_the_job_28_near_misses():
    spec = e2e._spec()
    assert on_watchlist(_score(30, 23, 12), spec)  # DigitalUp 65
    assert on_watchlist(_score(27, 19, 10), spec)  # OFFLINE 56
    assert not on_watchlist(_score(23, 27, 14), spec)  # WPP 64: fit 23, too big to buy outside capacity
    assert not on_watchlist(_score(29, 12, 12), spec)  # Media Service Italia 53: too far from the bar
    assert not on_watchlist(_score(35, 25, 15), spec)  # 75 qualifies outright
    assert not on_watchlist(_score(30, 23, 12), spec.model_copy(update={"include_watchlist": False}))


class NearMissLLM(e2e.FakeLLM):
    """Bravo scores 62: a near miss. Acme still qualifies (90)."""

    async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
        out = await super().parse(model, instructions, data, schema, effort, max_output_tokens)
        if schema is ScoreOut and "Bravo" in data:
            out.fit_score, out.intent_score, out.timing_score = 30, 20, 12
        return out


class CountingFinder(e2e.FakeFinder):
    domains: ClassVar[list[str]] = []

    async def find(self, first, last, domain, *args, **kwargs):
        CountingFinder.domains.append(domain)
        return await super().find(first, last, domain, *args, **kwargs)


def _run(tmp_path: Path, spec=None):
    with respx.mock:
        e2e._mock_sites()
        deps = e2e.Deps(
            e2e.FakeSerper(),
            NearMissLLM(),
            Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
            [CountingFinder()],
            e2e.FakeRegistry(),
        )
        return asyncio.run(e2e.run_job(spec or e2e._spec(), tmp_path / "out", deps=deps, today=e2e.TODAY))


def _no_mx(monkeypatch):
    async def mx(domain):
        return True

    monkeypatch.setattr(e2e.contacts, "has_mx", mx)


def test_a_near_miss_is_kept_on_its_own_sheet_and_never_delivered(tmp_path, clean_db, monkeypatch):
    _no_mx(monkeypatch)
    CountingFinder.domains = []
    summary = _run(tmp_path)
    assert (summary.qualified, summary.watchlist, summary.no_contact) == (1, 1, 0)
    assert "bravo.co.uk" not in CountingFinder.domains  # no paid email lookup for a lead that did not qualify

    wb = openpyxl.load_workbook(summary.outputs["xlsx"])
    watch = list(wb["Watchlist - near misses"].values)
    row = dict(zip(watch[0], watch[1], strict=True))
    assert len(watch) == 2
    assert row["company_domain"] == "bravo.co.uk"
    assert row["rank"] == 1
    assert row["why_not_qualified"].startswith("score 62 below 70")
    assert (row["fit_score"], row["intent_score"], row["timing_score"]) == (30, 20, 12)
    assert "bravo.co.uk" not in str(list(wb["Leads"].values))
    assert "bravo.co.uk" not in str(list(wb["Good leads - no contact"].values))
    assert any("Watchlist - near misses" in str(r) for r in wb["Methodology"].values)
    assert file_label(summary.outputs["watchlist_csv"]) == "Watchlist CSV"

    with session_scope() as s:
        bravo = s.scalar(select(JobLead).join(Company).where(Company.domain == "bravo.co.uk"))
        assert bravo.status == LeadStatus.WATCHLIST
        assert bravo.rank == 1
        delivered = set(s.scalars(select(Company.domain).join(Delivery, Delivery.company_id == Company.id)))
        counts = repo.lead_counts_for_jobs(s, [summary.job_id])[summary.job_id]
    assert "bravo.co.uk" not in delivered  # can still become a full lead in a later run
    assert "acme.co.uk" in delivered
    assert (counts.qualified, counts.watchlist, counts.delivered) == (1, 1, 1)


def test_a_watchlist_is_kept_in_a_signals_only_run_too(tmp_path, clean_db, monkeypatch):
    _no_mx(monkeypatch)
    spec = e2e._spec().model_copy(
        update={"output_mode": OutputMode.SIGNALS_ONLY, "personalized_email": False}
    )
    summary = _run(tmp_path, spec)
    assert (summary.qualified, summary.watchlist) == (1, 1)
    wb = openpyxl.load_workbook(summary.outputs["xlsx"])
    assert "bravo.co.uk" not in str(list(wb["Leads"].values))
    assert "bravo.co.uk" in str(list(wb["Watchlist - near misses"].values))


def test_switching_the_watchlist_off_rejects_near_misses_as_before(tmp_path, clean_db, monkeypatch):
    _no_mx(monkeypatch)
    summary = _run(tmp_path, e2e._spec().model_copy(update={"include_watchlist": False}))
    assert summary.watchlist == 0
    assert "watchlist_csv" not in summary.outputs
    assert "score below threshold" in summary.rejected


def test_the_job_page_lists_and_filters_the_watchlist(tmp_path, clean_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    _no_mx(monkeypatch)
    job_id = _run(tmp_path).job_id
    client = TestClient(app)
    page = client.get(f"/jobs/{job_id}")
    assert "Watchlist (near miss)" in page.text
    only = client.get(f"/jobs/{job_id}?tier=watchlist").text
    assert "bravo.co.uk" in only
    assert "acme.co.uk" not in only
    assert "bravo.co.uk" not in client.get(f"/jobs/{job_id}?tier=signal").text
    assert client.get(f"/jobs/{job_id}/progress").json()["watchlist"] == 1


# --- calibration -------------------------------------------------------------------------------------


def _rated(job: Job, s, n: int, total: tuple[int, int, int], verdict: str, prefix: str) -> None:
    for i in range(n):
        store.save_rejection(s, job, f"{prefix}{i}.co.uk", "GB", "placeholder", None)
        lead = s.scalar(select(JobLead).join(Company).where(Company.domain == f"{prefix}{i}.co.uk"))
        lead.status, lead.lead_tier, lead.reject_reason = LeadStatus.WATCHLIST, "signal", None
        lead.fit_score, lead.intent_score, lead.timing_score = total
        lead.total_score = sum(total)
        s.add(LeadFeedback(job_lead_id=lead.id, verdict=verdict))


def _job(s) -> Job:
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
    return job


def test_with_too_few_ratings_nothing_is_suggested(clean_db):
    with session_scope() as s:
        _rated(_job(s), s, 3, (30, 20, 12), "good", "a")
        report = fb.calibration(s, 70)
    assert report.rated == 3
    assert report.suggested is None
    assert "3 of 20 rated leads needed" in report.notes[0]
    assert next(b for b in report.bands if b.label == "60-69").good == 3


def test_good_ratings_on_near_misses_suggest_a_lower_bar(clean_db):
    with session_scope() as s:
        job = _job(s)
        _rated(job, s, 8, (32, 30, 13), "good", "q")  # 75: qualified and good
        _rated(job, s, 9, (30, 20, 12), "good", "w")  # 62: watchlist, rated good
        _rated(job, s, 2, (30, 20, 12), "wrong_intent", "x")
        _rated(job, s, 7, (25, 18, 12), "wrong_company", "y")  # 55: mostly wrong
        _rated(job, s, 1, (30, 20, 12), "wrong_contact", "z")  # a contact problem: not a scoring miss
        report = fb.calibration(s, 70)
    assert report.rated == 27
    # At 60+: 17 of 19 judged leads good (89%). At 55+: 17 of 26 (65%), under the 70% target.
    assert report.suggested == 60
    band = next(b for b in report.bands if b.label == "60-69")
    assert (band.good, band.wrong_intent, band.wrong_contact) == (9, 2, 1)
    assert band.precision == 9 / 11
    assert any("A bar of 60" in n for n in report.notes)


def test_mostly_wrong_company_points_at_fit_not_the_bar(clean_db):
    with session_scope() as s:
        job = _job(s)
        _rated(job, s, 6, (32, 30, 13), "good", "g")
        _rated(job, s, 14, (32, 30, 13), "wrong_company", "c")
        report = fb.calibration(s, 70)
    assert report.suggested is None
    assert any("fit score is too generous" in n for n in report.notes)


def test_the_dashboard_and_the_command_show_the_calibration(clean_db):
    from fastapi.testclient import TestClient

    from app.main import app

    assert "Is the score right?" in TestClient(app).get("/").text
    result = CliRunner().invoke(cli, ["calibrate"])
    assert result.exit_code == 0, result.output
    assert f"0 of {fb.MIN_RATED} rated leads needed" in result.output
    assert get_settings().watchlist_min_score < get_settings().qualify_score
