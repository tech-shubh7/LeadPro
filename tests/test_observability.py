"""Observability (2026-11-19): every model call is traced with its prompt, answer, tokens, price and time; each
run has a Trace tab, each call an inspector page, and the workspace an Observability page. Plus the calm light
theme. Fully offline: fake model, mocked web, no key."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config.database import session_scope
from app.config.settings import get_settings
from app.main import app
from app.models import Job, LlmCall
from app.providers.llm.llm_factory import create_llm
from app.providers.llm.llm_trace import TracingLLMProvider
from app.providers.web.page_fetcher import Fetcher, PageCache
from app.repositories import trace_repository
from app.services import observability_service
from app.services.extraction_service import ExtractOut
from app.utils.cost_tracker import CostTracker, TokenSplit, usage_tags
from tests import test_runner_e2e as e2e


class PricedLLM:
    """A fake model that records usage like the real providers do."""

    extract_model, judge_model = "gpt-6-luna", "gpt-6.1-terra"

    def __init__(self, cost: CostTracker, fail: bool = False, pause: float = 0.0):
        self.cost, self.fail, self.pause = cost, fail, pause

    async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
        await asyncio.sleep(self.pause)
        self.cost.record("openai", model, 1500, 0.25, TokenSplit(1000, 200, 500, 300))
        if self.fail:
            raise RuntimeError("model said no")
        return ExtractOut(
            sources_match_target=True,
            company_name="Acme",
            company_description="d",
            is_recruitment_or_staffing_firm=False,
            facts=[],
            signals=[],
            people=[],
        )


def test_a_traced_call_carries_its_prompt_answer_tokens_price_time_and_tags():
    traces = []
    cost = CostTracker(cap_inr=10)
    llm = TracingLLMProvider(PricedLLM(cost), traces.append)
    with usage_tags(job_id="7", stage="extract", domain="acme.com", channel="linkedin_jobs"):
        asyncio.run(llm.parse("gpt-6-luna", "Extract facts", "<source>page</source>", ExtractOut))
    t = traces[0]
    assert (t.job_id, t.stage, t.domain, t.channel) == (7, "extract", "acme.com", "linkedin_jobs")
    assert (t.model, t.schema_name, t.status, t.provider) == ("gpt-6-luna", "ExtractOut", "ok", "openai")
    assert (t.input_tokens, t.cached_tokens, t.output_tokens, t.reasoning_tokens) == (1000, 200, 500, 300)
    assert t.cost_inr == 0.25
    assert t.instructions == "Extract facts"
    assert t.input_text == "<source>page</source>"
    assert '"company_name": "Acme"' in t.output_text
    assert t.duration_ms >= 0
    assert not t.from_cache


def test_a_failed_call_is_traced_and_still_raises():
    traces = []
    llm = TracingLLMProvider(PricedLLM(CostTracker(cap_inr=10), fail=True), traces.append)
    with pytest.raises(RuntimeError):
        asyncio.run(llm.parse("m", "i", "d", ExtractOut))
    assert traces[0].status == "error"
    assert "model said no" in traces[0].error
    assert traces[0].cost_inr == 0.25  # the tokens were still paid for


def test_concurrent_calls_each_get_their_own_tokens():
    traces = []
    cost = CostTracker(cap_inr=10)

    async def go():
        slow = TracingLLMProvider(PricedLLM(cost, pause=0.02), traces.append)
        fast = TracingLLMProvider(PricedLLM(cost), traces.append)

        async def call(llm, domain):
            with usage_tags(domain=domain):
                await llm.parse("m", "i", "d", ExtractOut)

        await asyncio.gather(call(slow, "slow.com"), call(fast, "fast.com"))

    asyncio.run(go())
    assert sorted(t.domain for t in traces) == ["fast.com", "slow.com"]
    assert all(t.input_tokens == 1000 for t in traces)  # one call's usage each, never a neighbour's too


def test_a_sink_that_fails_never_fails_the_call():
    def broken(_):
        raise OSError("database down")

    llm = TracingLLMProvider(PricedLLM(CostTracker(cap_inr=10)), broken)
    assert asyncio.run(llm.parse("m", "i", "d", ExtractOut)).company_name == "Acme"


def test_long_prompts_are_cut_for_storage():
    traces = []
    llm = TracingLLMProvider(PricedLLM(CostTracker(cap_inr=10)), traces.append, max_chars=100)
    asyncio.run(llm.parse("m", "i" * 500, "d" * 500, ExtractOut))
    assert len(traces[0].instructions) == len(traces[0].input_text) == 100


def test_the_factory_traces_only_when_given_a_sink(monkeypatch):
    monkeypatch.setattr(get_settings(), "openai_api_key", "k")
    cost = CostTracker(cap_inr=10)
    assert isinstance(create_llm(cost, "openai", trace_sink=lambda t: None), TracingLLMProvider)
    assert not isinstance(create_llm(cost, "openai"), TracingLLMProvider)
    monkeypatch.setattr(get_settings(), "trace_llm_calls", False)
    assert not isinstance(create_llm(cost, "openai", trace_sink=lambda t: None), TracingLLMProvider)


# --- in a run, and on the pages ------------------------------------------------------------------------


def _traced_run(tmp_path: Path, monkeypatch) -> int:
    async def mx(domain):
        return True

    monkeypatch.setattr(e2e.contacts, "has_mx", mx)
    with respx.mock:
        e2e._mock_sites()
        deps = e2e.Deps(
            e2e.FakeSerper(),
            TracingLLMProvider(e2e.FakeLLM(), trace_repository.save_call),
            Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
            [e2e.FakeFinder()],
            e2e.FakeRegistry(),
        )
        return asyncio.run(e2e.run_job(e2e._spec(), tmp_path / "out", deps=deps, today=e2e.TODAY)).job_id


def test_every_model_call_in_a_run_is_stored_under_the_run(tmp_path, clean_db, monkeypatch):
    job_id = _traced_run(tmp_path, monkeypatch)
    with session_scope() as s:
        calls = list(s.scalars(select(LlmCall).where(LlmCall.job_id == job_id)))
        stages = {c.stage for c in calls}
        extract_domains = {c.domain for c in calls if c.stage == "extract"}
    assert {"discovery", "extract", "score"} <= stages
    assert extract_domains == {"acme.co.uk", "bravo.co.uk"}  # charlie was skipped free: no call to trace
    assert all(c.instructions for c in calls)


def test_the_trace_tab_the_call_page_and_the_observability_page_render(tmp_path, clean_db, monkeypatch):
    job_id = _traced_run(tmp_path, monkeypatch)
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.cost_lines = [
            {
                "provider": "openai",
                "operation": "gpt-6-luna",
                "units": 1500,
                "cost_inr": 0.25,
                "stage": "extract",
                "input_tokens": 1000,
                "output_tokens": 500,
                "reasoning_tokens": 300,
            },
            {"provider": "serper", "operation": "search", "units": 1, "cost_inr": 0.1, "stage": "discovery"},
        ]
        call_id = s.scalar(select(LlmCall.id).where(LlmCall.job_id == job_id, LlmCall.stage == "extract"))
    client = TestClient(app)
    trace = client.get(f"/jobs/{job_id}?tab=trace")
    assert trace.status_code == 200
    for text in ("Every model call", "By step", "acme.co.uk", "Output share of model spend"):
        assert text in trace.text
    only_score = client.get(f"/jobs/{job_id}?tab=trace&step=score").text
    assert "bravo.co.uk" in only_score  # scored companies only
    page = client.get(f"/calls/{call_id}")
    assert page.status_code == 200
    for text in ("What the model was told", "What it was given", "What it answered", "ExtractOut"):
        assert text in page.text
    assert client.get("/calls/999999").status_code == 404
    obs = client.get("/observability")
    assert obs.status_code == 200
    for text in ("Observability", "How it is set up", "Scoring", "Latest model calls", f"Run {job_id}"):
        assert text in obs.text


def test_old_traces_are_pruned(clean_db, monkeypatch):
    with session_scope() as s:
        s.add(
            LlmCall(
                model="m", schema_name="X", status="ok", created_at=datetime.now(UTC) - timedelta(days=40)
            )
        )
        s.add(LlmCall(model="m", schema_name="X", status="ok"))
    assert trace_repository.prune_traces() == 1


def test_usage_rollup_splits_spend_by_step_model_and_service():
    lines = [
        {
            "provider": "openai",
            "operation": "gpt-6-luna",
            "units": 1500,
            "cost_inr": 0.0264,
            "stage": "extract",
            "input_tokens": 1000,
            "output_tokens": 500,
            "reasoning_tokens": 300,
        },
        {
            "provider": "openai",
            "operation": "gpt-6.1-terra",
            "units": 2000,
            "cost_inr": 0.528,
            "stage": "score",
            "input_tokens": 1000,
            "output_tokens": 1000,
        },
        {"provider": "serper", "operation": "search", "units": 1, "cost_inr": 0.1, "stage": "discovery"},
        {"provider": "cache", "operation": "gpt-6-luna", "units": 0, "cost_inr": 0.0, "stage": "extract"},
        {"provider": "openai", "operation": "gpt-6-luna", "units": 100, "cost_inr": 0.01},  # old, untagged
    ]
    u = observability_service.usage(lines)
    assert u.cache_hits == 1
    assert u.models.calls == 3
    assert u.models.tokens == 3600
    assert round(u.other_inr, 4) == 0.1
    assert u.by_stage[0].name == "score"
    assert "untagged" in {b.name for b in u.by_stage}
    assert {b.name for b in u.by_provider} == {"openai", "serper"}
    share = u.output_share_of_spend
    assert share is not None
    assert 0.5 < share <= 1.0  # output dominates spend at 5x the price


# --- theme ---------------------------------------------------------------------------------------------


def test_the_console_has_one_calm_light_theme():
    css = Path("app/static/app.css").read_text()
    assert "prefers-color-scheme: dark" not in css  # no switch to dark: one light theme
    root = css[css.index(":root {") : css.index("}", css.index(":root {"))]
    for token in ("--surface: #", "--bg: #", "--ink: #"):
        value = root.split(token)[1][:6].lower()
        assert value not in ("ffffff", "000000")  # no pure white page, no pure black text
    assert 'content="light"' in Path("app/templates/base.html").read_text()
