"""Reuses model answers to identical questions, so repeated work is free.

Keyed by everything that determines the answer: model, instructions, input, output schema, effort and
output limit. A run continued with a bigger budget replays every company it already researched at no
cost and only pays for new ones. Answers are files under cache/llm/ and expire after LLM_CACHE_DAYS.
Evaluations bypass the cache: they exist to measure the model's variance, not to replay it.
"""

import hashlib
import json
import time
from pathlib import Path

from pydantic import ValidationError

from app.providers.llm.llm_provider import LLMProvider, T
from app.utils.logger import get_logger

logger = get_logger(__name__)
CACHE_DIR = Path("cache/llm")


class CachingLLMProvider:
    """Wraps any provider with an answer cache. Same interface, so callers cannot tell the difference."""

    def __init__(self, inner: LLMProvider, max_age_days: int, cache_dir: Path = CACHE_DIR):
        self.inner = inner
        self.max_age_s = max_age_days * 86400
        self.dir = cache_dir
        self.cost = getattr(inner, "cost", None)

    @property
    def extract_model(self) -> str:
        return self.inner.extract_model

    @property
    def judge_model(self) -> str:
        return self.inner.judge_model

    def _path(
        self, model: str, instructions: str, data: str, schema: type[T], effort: str, max_out: int
    ) -> Path:
        key = json.dumps(
            [model, instructions, data, schema.__name__, schema.model_json_schema(), effort, max_out]
        )
        return self.dir / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    async def parse(
        self,
        model: str,
        instructions: str,
        data: str,
        schema: type[T],
        effort: str = "low",
        max_output_tokens: int = 16000,
    ) -> T:
        path = self._path(model, instructions, data, schema, effort, max_output_tokens)
        try:
            if path.exists() and time.time() - path.stat().st_mtime < self.max_age_s:
                answer = schema.model_validate_json(path.read_text())
                if self.cost is not None:
                    self.cost.record("cache", model, 0, 0.0)  # visible on the run: reused, not paid
                return answer
        except (OSError, ValidationError):  # unreadable or from an older schema: ask again
            pass
        answer = await self.inner.parse(model, instructions, data, schema, effort, max_output_tokens)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(answer.model_dump_json())
        except OSError as exc:
            logger.warning("could not cache a model answer: %s", exc)
        return answer
