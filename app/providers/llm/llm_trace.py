"""Records every model call for the console's Trace view: what was asked, what came back, tokens, price, time.

Wraps any provider (outermost, so a cached answer is traced too, at zero cost). The trace goes to a sink the
caller passes in (the run saves it to the llm_calls table); providers never touch the database themselves.
A sink that fails is logged and ignored: observability must never cost a lead.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.providers.llm.llm_provider import LLMProvider, T
from app.utils.cost_tracker import capture_usage, current_tags
from app.utils.logger import get_logger

logger = get_logger(__name__)
MODEL_PROVIDERS = ("openai", "gemini", "manual", "cache")


@dataclass
class CallTrace:
    job_id: int | None
    stage: str
    channel: str
    domain: str
    provider: str
    model: str
    schema_name: str
    effort: str
    status: str  # "ok" | "error"
    error: str
    duration_ms: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_inr: float
    from_cache: bool
    instructions: str
    input_text: str
    output_text: str


TraceSink = Callable[[CallTrace], None]


class TracingLLMProvider:
    """Same interface as the provider it wraps; callers cannot tell the difference."""

    def __init__(self, inner: LLMProvider, sink: TraceSink, max_chars: int = 20000):
        self.inner, self.sink, self.max_chars = inner, sink, max_chars
        self.cost = getattr(inner, "cost", None)

    @property
    def extract_model(self) -> str:
        return self.inner.extract_model

    @property
    def judge_model(self) -> str:
        return self.inner.judge_model

    async def parse(
        self,
        model: str,
        instructions: str,
        data: str,
        schema: type[T],
        effort: str = "low",
        max_output_tokens: int = 16000,
    ) -> T:
        started = time.monotonic()
        tags = current_tags()
        with capture_usage() as lines:
            try:
                answer = await self.inner.parse(model, instructions, data, schema, effort, max_output_tokens)
            except Exception as exc:
                await self._emit(tags, model, schema, effort, instructions, data, None, exc, started, lines)
                raise
        await self._emit(tags, model, schema, effort, instructions, data, answer, None, started, lines)
        return answer

    async def _emit(self, tags, model, schema, effort, instructions, data, answer, error, started, lines):
        used = [line for line in lines if line.provider in MODEL_PROVIDERS]
        trace = CallTrace(
            job_id=int(tags["job_id"]) if tags.get("job_id", "").isdigit() else None,
            stage=tags.get("stage", ""),
            channel=tags.get("channel", ""),
            domain=tags.get("domain", ""),
            provider=used[-1].provider if used else "",
            model=model,
            schema_name=schema.__name__,
            effort=effort,
            status="error" if error else "ok",
            error=f"{type(error).__name__}: {error}"[: self.max_chars] if error else "",
            duration_ms=int((time.monotonic() - started) * 1000),
            input_tokens=sum(line.input_tokens for line in used),
            cached_tokens=sum(line.cached_tokens for line in used),
            output_tokens=sum(line.output_tokens for line in used),
            reasoning_tokens=sum(line.reasoning_tokens for line in used),
            cost_inr=round(sum(line.cost_inr for line in used), 6),
            from_cache=any(line.provider == "cache" for line in used),
            instructions=instructions[: self.max_chars],
            input_text=data[: self.max_chars],
            output_text=answer.model_dump_json(indent=1)[: self.max_chars] if answer is not None else "",
        )
        try:
            await asyncio.to_thread(self.sink, trace)
        except Exception:  # never fail a model call because its trace could not be stored
            logger.exception("could not store the trace of a %s call", schema.__name__)
