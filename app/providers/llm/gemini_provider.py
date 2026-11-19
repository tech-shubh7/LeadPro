"""Google Gemini, behind the same `parse` interface as the OpenAI provider.

Used when there is no OpenAI key. The google-genai SDK takes the Pydantic model directly as
`response_schema` and returns a validated instance, so the structured-output guarantee the pipeline
relies on (and the quote/date checks that run on top of it) is unchanged.

Two models, the same split the pipeline expects: a cheap one for high-volume extraction and triage,
a strong one for judging. Thinking is off for extraction (it is copying, not reasoning) and on for
the judge steps.
"""

import asyncio
import json
import random
import re
import time

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.config.settings import get_settings
from app.exceptions.custom_exceptions import LLMRefusalError
from app.providers.llm.openai_provider import DATA_GUARD, T, wrap_data
from app.utils.cost_tracker import CostTracker, TokenSplit
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Thinking budget in tokens per effort level. 0 disables it; the judge steps get room to reason.
THINKING_BUDGET = {"low": 0, "medium": 4096, "high": 8192}
# Models that cannot turn thinking off entirely (a budget of 0 is rejected).
_ALWAYS_THINKS = ("-pro",)
# Transient on the provider's side: worth retrying. 503 ("experiencing high demand") and 429 (rate
# limited) are both routine on a shared endpoint, and losing a whole run to one is not acceptable.
RETRY_CODES = frozenset({429, 500, 502, 503, 504})
ATTEMPTS = 4
BASE_DELAY_S = 2.0
REQUEST_OVERHEAD_TOKENS = 1024  # provider framing/tooling; deliberately pessimistic
# Per call. The SDK sets no timeout, and an overloaded endpoint can hold a request open indefinitely: one
# such call once froze a plan draft for minutes with nothing logged. A stalled model goes straight to the
# next one in the fallback chain rather than being waited on again.
CALL_TIMEOUT_S = 90.0
# Circuit breaker: a model that just exhausted its retries or timed out is skipped for this long, so the
# rest of a run goes straight to the model that is answering instead of re-paying the retries per call.
COOLDOWN_S = 300.0
_cooling: dict[str, float] = {}  # model -> monotonic time it may be tried again (process-wide)


_MUST_THINK: set[str] = set()  # models seen rejecting thinking_budget=0 (process-wide)


def _thinking_is_off(config) -> bool:
    tc = getattr(config, "thinking_config", None)
    return tc is not None and tc.thinking_budget == 0


def _without_thinking_off(config):
    return config.model_copy(update={"thinking_config": None}) if _thinking_is_off(config) else config


MAX_RETRY_WAIT_S = 65.0
_RETRY_IN = re.compile(r"retry in ([\d.]+)\s*s|'retryDelay':\s*'(\d+)s'", re.I)


def _retry_after(exc: Exception) -> float | None:
    if m := _RETRY_IN.search(str(exc)):
        return float(m.group(1) or m.group(2))
    return None


def _cool(model: str) -> None:
    _cooling[model] = time.monotonic() + COOLDOWN_S


class GeminiProvider:
    def __init__(self, cost: CostTracker, client: genai.Client | None = None):
        self.settings = get_settings()
        self.cost = cost
        # v1beta: the current model generation is not served on v1, which answers 404 for all of them.
        self.client = client or genai.Client(
            api_key=self.settings.gemini_api_key, http_options=types.HttpOptions(api_version="v1beta")
        )

    @property
    def extract_model(self) -> str:
        return self.settings.gemini_extract_model

    @property
    def judge_model(self) -> str:
        return self.settings.gemini_judge_model

    def _price_inr(self, model: str, usage) -> float:
        p_in, _, p_out = self.settings.price_for(model)
        tokens_in = usage.prompt_token_count or 0
        # Thinking tokens are billed as output but reported separately from the visible answer.
        tokens_out = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        return (tokens_in * p_in + tokens_out * p_out) / 1_000_000 * self.settings.usd_to_inr

    def _budget(self, model: str, effort: str) -> int:
        budget = THINKING_BUDGET.get(effort, 0)
        if budget == 0 and any(m in model for m in _ALWAYS_THINKS):
            return 128  # the minimum this family accepts
        return budget

    async def _generate_with_retry(self, model: str, data: str, config):
        """Retries the provider's own transient failures with backoff, then moves to the next model in
        `gemini_fallback_models` if this one stays overloaded. Nothing is charged for a failed call, so a
        retry costs latency only; the per-job cap is still held by the caller's reservation.
        Returns (response, model that answered)."""
        full = [model, *[m for m in self.settings.gemini_fallback_models if m != model]]
        now = time.monotonic()
        chain = [m for m in full if _cooling.get(m, 0) <= now] or full  # all cooling: try them anyway
        last: Exception | None = None
        for i, current in enumerate(chain):
            attempts = ATTEMPTS if i == 0 else 2
            cfg = _without_thinking_off(config) if current in _MUST_THINK else config
            attempt = 0
            while attempt < attempts:
                try:
                    resp = await asyncio.wait_for(
                        self.client.aio.models.generate_content(
                            model=current, contents=wrap_data(data), config=cfg
                        ),
                        CALL_TIMEOUT_S,
                    )
                    if current != model:
                        logger.warning("gemini %s was unavailable; %s answered instead", model, current)
                    _cooling.pop(current, None)
                    return resp, current
                except genai_errors.APIError as exc:
                    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                    last = exc
                    if code == 400 and _thinking_is_off(cfg):
                        # Some models cannot turn thinking off and reject budget 0 as an invalid argument
                        # (gemini-3.5-flash-lite does). Ask again with the model's default thinking.
                        _MUST_THINK.add(current)
                        cfg = _without_thinking_off(cfg)
                        logger.warning(
                            "gemini %s cannot disable thinking; retrying with its default", current
                        )
                        continue  # same attempt count: this was our request's shape, not an outage
                    if code not in RETRY_CODES and code != 404:
                        if current == model:
                            raise LLMRefusalError(f"gemini {current}: {exc}") from exc
                        # A fallback that cannot serve this request: try the next one, do not fail the step.
                        logger.warning(
                            "gemini fallback %s refused the request (%s); trying the next", current, code
                        )
                        _cool(current)
                        break
                    if code == 404 or attempt == attempts - 1:
                        _cool(current)
                        break  # this model is gone or still overloaded: try the next one
                    delay = BASE_DELAY_S * (2**attempt) + random.uniform(0, 1)
                    if code == 429 and (asked := _retry_after(exc)):
                        # A per-minute quota says exactly when it resets ("retry in 43.9s"); retrying
                        # sooner just fails again (job 18 lost five agencies that way).
                        delay = min(max(delay, asked + random.uniform(0.5, 2)), MAX_RETRY_WAIT_S)
                    logger.warning(
                        "gemini %s returned %s, retrying in %.1fs (%d/%d)",
                        current,
                        code,
                        delay,
                        attempt + 1,
                        attempts - 1,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                except (TimeoutError, httpx.TransportError) as exc:
                    last = exc  # stalled or dropped connection: do not wait on this model again
                    logger.warning("gemini %s: %s", current, type(exc).__name__)
                    _cool(current)
                    break
        raise LLMRefusalError(f"gemini {model} and its fallbacks are unavailable: {last}") from last

    async def parse(
        self,
        model: str,
        instructions: str,
        data: str,
        schema: type[T],
        effort: str = "low",
        max_output_tokens: int = 16000,
    ) -> T:
        # Held against the cap while the call is in flight, so it must not undershoot what the call can
        # cost: price at the dearest model the call may fall back to, and count the thinking budget,
        # which is billed as output. An undershoot let a run end 10% over its cap.
        chain = [model, *self.settings.gemini_fallback_models]
        p_in = max(self.settings.price_for(m)[0] for m in chain)
        p_out = max(self.settings.price_for(m)[2] for m in chain)
        # Reserve the maximum billable request, not an average-token estimate.
        # Thinking tokens and visible output are both billable and may each reach
        # their configured limits.
        schema_bytes = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        input_tokens = (
            len((instructions + "\n" + wrap_data(data) + DATA_GUARD + schema_bytes).encode("utf-8"))
            + REQUEST_OVERHEAD_TOKENS
        )
        out_tokens = max_output_tokens + self._budget(model, effort)
        estimate = (input_tokens * p_in + out_tokens * p_out) / 1_000_000 * self.settings.usd_to_inr
        async with self.cost.hold(estimate):
            config = types.GenerateContentConfig(
                system_instruction=f"{instructions}\n\n{DATA_GUARD}",
                response_mime_type="application/json",
                response_schema=schema,
                max_output_tokens=max_output_tokens,
                thinking_config=types.ThinkingConfig(thinking_budget=self._budget(model, effort)),
            )
            resp, answered = await self._generate_with_retry(model, data, config)
            if resp.usage_metadata:
                self.cost.record(
                    "gemini",
                    answered,
                    resp.usage_metadata.total_token_count or 0,
                    self._price_inr(answered, resp.usage_metadata),
                    _split(resp.usage_metadata),
                )
        parsed = resp.parsed
        if isinstance(parsed, schema):
            return parsed
        # The SDK returns None when the answer was cut short or blocked; re-validate the raw text so the
        # reason is specific rather than "no output".
        if resp.text:
            try:
                return schema.model_validate_json(resp.text)
            except ValidationError as exc:
                raise LLMRefusalError(
                    f"gemini {model} returned JSON that does not match {schema.__name__}: {exc}"
                ) from exc
        reason = (
            getattr(getattr(resp, "candidates", [None])[0], "finish_reason", None)
            if resp.candidates
            else None
        )
        raise LLMRefusalError(f"gemini {model} returned no output (finish_reason={reason})")


def is_configured() -> bool:
    return bool(get_settings().gemini_api_key)


__all__ = ["BaseModel", "GeminiProvider", "is_configured"]


def _split(usage) -> TokenSplit:
    thoughts = usage.thoughts_token_count or 0
    return TokenSplit(
        usage.prompt_token_count or 0,
        getattr(usage, "cached_content_token_count", 0) or 0,
        (usage.candidates_token_count or 0) + thoughts,
        thoughts,
    )
