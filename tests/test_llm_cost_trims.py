"""Model spend: output costs 5x input, and on jobs 24-28 output + reasoning was 18% of the tokens but 56% of
the money (derived from each call's stored price, 2026-11-19). So every call now records its split, the
extraction answer is kept short, and the intent check is skipped where it cannot change the outcome.
Fully offline: fake clients, no key."""

import asyncio
from types import SimpleNamespace

import httpx
import respx

from app.providers.llm.gemini_provider import _split as gemini_split
from app.providers.llm.openai_provider import OpenAIProvider
from app.services.extraction_service import MAX_FACTS, ExtractOut, QuotedFact
from app.services.signal_intelligence_service import IntentOut
from app.utils.cost_tracker import CostTracker, TokenSplit, UsageLine
from tests import test_runner_e2e as e2e


class FakeResponses:
    def __init__(self, usage):
        self.usage = usage

    async def parse(self, **kwargs):
        return SimpleNamespace(usage=self.usage, status="completed", output_parsed=SimpleNamespace(ok=True))


def test_an_openai_call_records_input_cached_output_and_reasoning():
    usage = SimpleNamespace(
        input_tokens=3000,
        output_tokens=900,
        total_tokens=3900,
        input_tokens_details=SimpleNamespace(cached_tokens=1024),
        output_tokens_details=SimpleNamespace(reasoning_tokens=600),
    )
    cost = CostTracker(cap_inr=10)
    provider = OpenAIProvider(cost, client=SimpleNamespace(responses=FakeResponses(usage)))
    asyncio.run(provider.parse("gpt-6-luna", "i", "d", ExtractOut, max_output_tokens=100))
    line = cost.lines[0]
    assert (line.units, line.input_tokens, line.cached_tokens) == (3900, 3000, 1024)
    assert (line.output_tokens, line.reasoning_tokens) == (900, 600)
    assert line.cost_inr == provider._price_inr("gpt-6-luna", usage)


def test_an_openai_call_without_details_still_records():
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15, input_tokens_details=None)
    cost = CostTracker(cap_inr=10)
    asyncio.run(
        OpenAIProvider(cost, client=SimpleNamespace(responses=FakeResponses(usage))).parse(
            "gpt-6-luna", "i", "d", ExtractOut, max_output_tokens=100
        )
    )
    assert (cost.lines[0].cached_tokens, cost.lines[0].reasoning_tokens) == (0, 0)


def test_gemini_thinking_counts_as_output():
    usage = SimpleNamespace(
        prompt_token_count=2000,
        candidates_token_count=300,
        thoughts_token_count=500,
        cached_content_token_count=None,
    )
    assert gemini_split(usage) == TokenSplit(2000, 0, 800, 500)


def test_non_model_lines_and_old_lines_have_no_split():
    cost = CostTracker(cap_inr=10)
    cost.record("serper", "search", 1, 0.1)
    assert cost.lines[0].input_tokens == cost.lines[0].output_tokens == 0
    assert UsageLine("openai", "m", 1, 0.1).reasoning_tokens == 0


def test_the_extraction_answer_is_asked_to_be_short():
    schema = str(ExtractOut.model_json_schema())
    assert f"At most {MAX_FACTS} firmographic facts" in schema
    fields = QuotedFact.model_json_schema()["properties"]
    assert "at most 15 words" in fields["claim"]["description"]
    assert "6 to 40 words" in fields["quote"]["description"]


def test_no_intent_call_for_a_company_without_a_fresh_signal(tmp_path, clean_db, monkeypatch):
    """Bravo has a fresh dated page, so the date gate lets it through, but extraction finds no signal on it:
    it is rejected without asking the intent model, which could not have changed that."""
    asked: list[str] = []

    class NoFreshForBravo(e2e.FakeLLM):
        async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
            if schema is IntentOut:
                asked.append(data)  # recorded before the base fake (which has no intent answer) raises
            out = await super().parse(model, instructions, data, schema, effort, max_output_tokens)
            if schema is ExtractOut and "website is bravo.co.uk" in instructions:
                out.signals = []  # nothing fresh found on its pages
            return out

    async def mx(domain):
        return True

    monkeypatch.setattr(e2e.contacts, "has_mx", mx)
    from app.providers.web.page_fetcher import Fetcher, PageCache

    with respx.mock:
        e2e._mock_sites()
        deps = e2e.Deps(
            e2e.FakeSerper(),
            NoFreshForBravo(),
            Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
            [e2e.FakeFinder()],
            e2e.FakeRegistry(),
        )
        summary = asyncio.run(e2e.run_job(e2e._spec(), tmp_path / "out", deps=deps, today=e2e.TODAY))
    assert not any("Bravo" in d for d in asked)
    assert any("Acme" in d for d in asked)  # a company with a fresh signal is still checked
    assert summary.qualified == 1
    assert summary.rejected.get("no verified buying sign dated within 90 days") == 1


def test_the_usage_command_splits_a_run_by_step(clean_db):
    from typer.testing import CliRunner

    from app.cli import cli
    from app.config.database import session_scope
    from app.models import Job

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
        job.cost_lines = [
            {
                "provider": "openai",
                "operation": "m",
                "units": 3900,
                "cost_inr": 0.5,
                "stage": "extract",
                "input_tokens": 3000,
                "cached_tokens": 1024,
                "output_tokens": 900,
                "reasoning_tokens": 600,
            },
            {"provider": "serper", "operation": "search", "units": 1, "cost_inr": 0.1, "stage": "discovery"},
            {"provider": "openai", "operation": "m", "units": 100, "cost_inr": 0.2},  # an old, untagged line
        ]
        s.add(job)
        s.flush()
        job_id = job.id
    out = CliRunner().invoke(cli, ["usage", str(job_id)])
    assert out.exit_code == 0, out.output
    extract = next(line for line in out.output.splitlines() if line.startswith("extract"))
    assert extract.split()[1:6] == ["1", "3,900", "3,000", "1,024", "900"]
    assert "untagged" in out.output
    assert "discovery" not in out.output  # a search is not a model call
