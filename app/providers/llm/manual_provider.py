"""Hybrid mode: LLM steps are answered through files instead of the OpenAI API.

Each call writes work/llm_tasks/<id>.task.json (instructions, untrusted data, JSON schema) and waits
for <id>.response.json. Responses are validated against the same Pydantic schema, and everything
downstream (quote verification, date rules, gates) is identical to OpenAI mode. Answers are keyed by
a hash of the request, so re-running the same job reuses them.
"""
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.core.cost import CostTracker
from app.providers.llm.openai_provider import DATA_GUARD, LLMRefusal

T = TypeVar("T", bound=BaseModel)
TASK_DIR = Path("work/llm_tasks")


class ManualLLMProvider:
    def __init__(self, cost: CostTracker, task_dir: Path = TASK_DIR, poll_s: float = 2.0, timeout_s: float = 4 * 3600):
        self.cost = cost
        self.task_dir = task_dir
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        task_dir.mkdir(parents=True, exist_ok=True)

    async def parse(self, model: str, instructions: str, data: str, schema: type[T], effort: str = "low",
                    max_output_tokens: int = 16000) -> T:
        task_id = hashlib.sha256(f"{schema.__name__}\n{instructions}\n{data}".encode()).hexdigest()[:16]
        task_path = self.task_dir / f"{task_id}.task.json"
        resp_path = self.task_dir / f"{task_id}.response.json"
        err_path = self.task_dir / f"{task_id}.error.txt"
        if not resp_path.exists() and not task_path.exists():
            task_path.write_text(json.dumps({
                "id": task_id, "step": schema.__name__, "role_model": model, "effort": effort,
                "instructions": f"{instructions}\n\n{DATA_GUARD}", "json_schema": schema.model_json_schema(),
                "data": data, "created_at": datetime.now(UTC).isoformat(),
            }, indent=1))
        waited = 0.0
        while waited < self.timeout_s:
            if resp_path.exists():
                try:
                    parsed = schema.model_validate_json(resp_path.read_text())
                except ValidationError as exc:
                    err_path.write_text(str(exc))
                    resp_path.rename(resp_path.with_suffix(".rejected.json"))
                else:
                    err_path.unlink(missing_ok=True)
                    task_path.unlink(missing_ok=True)  # answered; response file stays as the cache
                    self.cost.record("manual", schema.__name__, 1, 0.0)
                    return parsed
            await asyncio.sleep(self.poll_s)
            waited += self.poll_s
        raise LLMRefusal(f"no response for manual task {task_id} within {self.timeout_s:.0f}s")
