"""Gemini provider: an overloaded model falls back to the next one instead of losing the step.
No network: the SDK client is a fake."""
import asyncio
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors
from pydantic import BaseModel

from app.core.cost import CostTracker
from app.providers.llm import gemini_provider as gp
from app.providers.llm.openai_provider import LLMRefusal


class Out(BaseModel):
    answer: str


def _busy(model):
    return genai_errors.APIError(503, {"error": {"code": 503, "message": f"{model} busy", "status": "UNAVAILABLE"}})


class FakeModels:
    def __init__(self, busy: set[str]):
        self.busy, self.calls = busy, []

    async def generate_content(self, model, contents, config):
        self.calls.append(model)
        if model in self.busy:
            raise _busy(model)
        usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=10, thoughts_token_count=0,
                                total_token_count=110)
        return SimpleNamespace(parsed=Out(answer=model), text=None, usage_metadata=usage, candidates=[])


def _provider(busy: set[str]):
    gp._cooling.clear()
    models = FakeModels(busy)
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return gp.GeminiProvider(CostTracker(cap_inr=100), client=client), models


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(gp.asyncio, "sleep", instant)


def test_an_overloaded_model_falls_back_to_the_next(monkeypatch):
    provider, models = _provider(busy={"gemini-3.8-flash"})
    monkeypatch.setattr(provider.settings, "gemini_fallback_models", ["gemini-3.7-flash"])
    out = asyncio.run(provider.parse("gemini-3.8-flash", "i", "d", Out))
    assert out.answer == "gemini-3.7-flash"
    assert models.calls.count("gemini-3.8-flash") == gp.ATTEMPTS
    assert provider.cost.lines[-1].operation == "gemini-3.7-flash"  # charged to the model that answered


def test_every_model_busy_is_a_refusal(monkeypatch):
    provider, _ = _provider(busy={"gemini-3.8-flash", "gemini-3.7-flash"})
    monkeypatch.setattr(provider.settings, "gemini_fallback_models", ["gemini-3.7-flash"])
    with pytest.raises(LLMRefusal, match="fallbacks are unavailable"):
        asyncio.run(provider.parse("gemini-3.8-flash", "i", "d", Out))
    assert provider.cost.reserved_inr == 0


def test_a_model_that_never_answers_is_abandoned_for_the_next(monkeypatch):
    """The SDK has no timeout; a hung call once froze a plan draft indefinitely."""
    provider, models = _provider(busy=set())
    real = models.generate_content

    async def hang_on_primary(model, contents, config):
        if model == "gemini-3.8-flash":
            models.calls.append(model)
            await asyncio.Event().wait()  # never returns
        return await real(model, contents, config)

    models.generate_content = hang_on_primary
    monkeypatch.setattr(gp, "CALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(provider.settings, "gemini_fallback_models", ["gemini-3.7-flash"])
    out = asyncio.run(provider.parse("gemini-3.8-flash", "i", "d", Out))
    assert out.answer == "gemini-3.7-flash" and models.calls.count("gemini-3.8-flash") == 1


def test_a_model_that_failed_over_is_skipped_for_a_while(monkeypatch):
    monkeypatch.setattr(gp, "_cooling", {})
    provider, models = _provider(busy={"gemini-3.8-flash"})
    monkeypatch.setattr(provider.settings, "gemini_fallback_models", ["gemini-3.7-flash"])
    asyncio.run(provider.parse("gemini-3.8-flash", "i", "d", Out))
    models.calls.clear()
    out = asyncio.run(provider.parse("gemini-3.8-flash", "i", "d", Out))
    assert out.answer == "gemini-3.7-flash" and models.calls == ["gemini-3.7-flash"]  # no retries re-paid


def _invalid(model):
    return genai_errors.APIError(400, {"error": {"code": 400, "message": "Request contains an invalid argument.",
                                                 "status": "INVALID_ARGUMENT"}})


def test_a_model_that_cannot_disable_thinking_is_asked_again_with_its_default(monkeypatch):
    """Job 16: gemini-3.5-flash-lite rejects thinking_budget=0 with 400; 7 of 8 companies were lost."""
    provider, models = _provider(busy={"gemini-3.8-flash"})
    monkeypatch.setattr(provider.settings, "gemini_fallback_models", ["gemini-3.5-flash-lite"])
    monkeypatch.setattr(gp, "_MUST_THINK", set())
    real = models.generate_content

    async def lite_needs_thinking(model, contents, config):
        thinking = config.thinking_config
        if model == "gemini-3.5-flash-lite" and thinking is not None and thinking.thinking_budget == 0:
            models.calls.append(model)
            raise _invalid(model)
        return await real(model, contents, config)

    models.generate_content = lite_needs_thinking
    out = asyncio.run(provider.parse("gemini-3.8-flash", "i", "d", Out, effort="low"))
    assert out.answer == "gemini-3.5-flash-lite" and "gemini-3.5-flash-lite" in gp._MUST_THINK


def test_a_fallback_that_refuses_the_request_hands_on_to_the_next(monkeypatch):
    provider, models = _provider(busy={"gemini-3.8-flash"})
    monkeypatch.setattr(provider.settings, "gemini_fallback_models", ["gemini-x-broken", "gemini-3.6-flash"])
    real = models.generate_content

    async def broken(model, contents, config):
        if model == "gemini-x-broken":
            raise genai_errors.APIError(400, {"error": {"code": 400, "message": "bad", "status": "FAILED_PRECONDITION"}})
        return await real(model, contents, config)

    models.generate_content = broken
    out = asyncio.run(provider.parse("gemini-3.8-flash", "i", "d", Out, effort="medium"))
    assert out.answer == "gemini-3.6-flash"


def test_the_primary_model_refusing_the_request_still_fails_loudly(monkeypatch):
    provider, models = _provider(busy=set())

    async def bad(model, contents, config):
        raise genai_errors.APIError(400, {"error": {"code": 400, "message": "bad", "status": "FAILED_PRECONDITION"}})

    models.generate_content = bad
    with pytest.raises(LLMRefusal):
        asyncio.run(provider.parse("gemini-3.8-flash", "i", "d", Out, effort="medium"))
