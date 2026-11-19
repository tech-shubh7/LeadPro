"""Builds the configured LLM provider. The single place that maps a mode name to a class."""

from app.config.settings import get_settings
from app.providers.llm.gemini_provider import GeminiProvider
from app.providers.llm.llm_cache import CachingLLMProvider
from app.providers.llm.llm_provider import LLMProvider
from app.providers.llm.llm_trace import TraceSink, TracingLLMProvider
from app.providers.llm.manual_provider import ManualLLMProvider
from app.providers.llm.openai_provider import OpenAIProvider
from app.utils.cost_tracker import CostTracker

# 'openai' and 'gemini' call their APIs; 'manual' writes each step to work/llm_tasks for an operator to
# answer. Everything after the model (quote checks, dates, gates) is identical, so the mode changes who
# writes the JSON, never what is trusted.
_PROVIDERS = {"openai": OpenAIProvider, "gemini": GeminiProvider, "manual": ManualLLMProvider}
LLM_MODES = tuple(_PROVIDERS)


def has_credentials(mode: str) -> bool:
    settings = get_settings()
    return {"openai": bool(settings.openai_api_key), "gemini": bool(settings.gemini_api_key)}.get(mode, True)


def create_llm(
    cost: CostTracker, mode: str | None = None, cache: bool = True, trace_sink: TraceSink | None = None
) -> LLMProvider:
    """`trace_sink` receives every call (prompt, answer, tokens, price, time) when tracing is on."""
    settings = get_settings()
    mode = mode or settings.llm_mode
    if mode not in _PROVIDERS:
        raise ValueError(f"llm mode must be one of {LLM_MODES}, got {mode!r}")
    provider = _PROVIDERS[mode](cost)
    if cache and mode != "manual" and settings.llm_cache_days > 0:  # manual mode keeps its own answer files
        provider = CachingLLMProvider(provider, settings.llm_cache_days)
    if trace_sink is not None and settings.trace_llm_calls:  # outermost: a cached answer is traced too
        provider = TracingLLMProvider(provider, trace_sink, settings.trace_max_chars)
    return provider
