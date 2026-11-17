"""Hybrid mode: LLM steps are answered through files instead of the OpenAI API.

Each call writes work/llm_tasks/<id>.task.json (instructions, untrusted data, JSON schema) and waits
for <id>.response.json. Responses are validated against the same Pydantic schema, and everything
downstream (quote verification, date rules, gates) is identical to OpenAI mode. Answers are keyed by
a hash of the request, so re-running the same job reuses them.
"""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.config.settings import get_settings
from app.exceptions.custom_exceptions import LLMRefusalError
from app.providers.llm.openai_provider import DATA_GUARD, wrap_data
from app.utils.cost_tracker import CostTracker

T = TypeVar("T", bound=BaseModel)
TASK_DIR = Path("work/llm_tasks")


class ManualLLMProvider:
    def __init__(
        self, cost: CostTracker, task_dir: Path = TASK_DIR, poll_s: float = 2.0, timeout_s: float = 4 * 3600
    ):
        self.cost = cost
        self.task_dir = task_dir
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        task_dir.mkdir(parents=True, exist_ok=True)

    # Only labels here: the model name is written into the task file so whoever answers knows which
    # role the step plays. Nothing is called.
    @property
    def extract_model(self) -> str:
        return get_settings().llm_extract_model

    @property
    def judge_model(self) -> str:
        return get_settings().llm_judge_model

    async def parse(
        self,
        model: str,
        instructions: str,
        data: str,
        schema: type[T],
        effort: str = "low",
        max_output_tokens: int = 16000,
    ) -> T:
        task_id = hashlib.sha256(f"{schema.__name__}\n{instructions}\n{data}".encode()).hexdigest()[:16]
        task_path = self.task_dir / f"{task_id}.task.json"
        resp_path = self.task_dir / f"{task_id}.response.json"
        err_path = self.task_dir / f"{task_id}.error.txt"
        task = {
            "id": task_id,
            "step": schema.__name__,
            "role_model": model,
            "effort": effort,
            "instructions": f"{instructions}\n\n{DATA_GUARD}",
            "json_schema": schema.model_json_schema(),
            "data": wrap_data(data),
            "created_at": datetime.now(UTC).isoformat(),
        }
        if not resp_path.exists() and not task_path.exists():
            task_path.write_text(json.dumps(task, indent=1))
        waited = 0.0
        while waited < self.timeout_s:
            if resp_path.exists():
                try:
                    parsed = schema.model_validate_json(resp_path.read_text())
                except ValidationError as exc:
                    err_path.write_text(str(exc))
                    resp_path.rename(resp_path.with_suffix(".rejected.json"))
                    # A cached answer that no longer validates had no task file: re-list the task, or the
                    # operator never sees it and the run just waits out the timeout.
                    if not task_path.exists():
                        task_path.write_text(json.dumps(task, indent=1))
                else:
                    err_path.unlink(missing_ok=True)
                    task_path.unlink(missing_ok=True)  # answered; response file stays as the cache
                    self.cost.record("manual", schema.__name__, 1, 0.0)
                    return parsed
            await asyncio.sleep(self.poll_s)
            waited += self.poll_s
        raise LLMRefusalError(f"no response for manual task {task_id} within {self.timeout_s:.0f}s")


# ---- operator side: inspect and answer tasks (used by `leadpro task` / `leadpro answer`) ------


def pending_tasks(task_dir: Path = TASK_DIR) -> list[Path]:
    return sorted(task_dir.glob("*.task.json"), key=lambda p: p.stat().st_mtime) if task_dir.exists() else []


def check_answer(task: dict, answer: dict) -> list[str]:
    """Pre-checks an extraction answer with the pipeline's own quote rules, so a bad quote is fixed now
    instead of being silently dropped later. (The pipeline re-verifies everything against full page text.)"""
    from app.utils.evidence_verifier import phrase_near_quote, quote_in_text

    if task["step"] == "DraftOut":
        from app.services.draft_service import MAX_BODY_WORDS

        words = len(answer.get("body", "").split())
        return (
            [f"draft body is {words} words; the pipeline rejects more than {MAX_BODY_WORDS}"]
            if words > MAX_BODY_WORDS
            else []
        )
    if task["step"] != "ExtractOut":
        return []
    sources = dict(re.findall(r'<source url="([^"]+)">\n(.*?)\n</source>', task["data"], re.S))
    problems = []
    for kind in ("facts", "signals", "people"):
        for item in answer.get(kind, []):
            text = sources.get(item.get("source_url", ""))
            if text is None:
                problems.append(f"{kind}: unknown source_url {item.get('source_url')}")
            elif not quote_in_text(item.get("quote", ""), text):
                problems.append(f"{kind}: quote not found verbatim: {item.get('quote', '')[:80]}")
            elif (
                kind == "signals"
                and item.get("event_date_quote")
                and not phrase_near_quote(item["event_date_quote"], item["quote"], text)
            ):
                problems.append(f"signals: event date phrase not near the quote: {item['event_date_quote']}")
            elif kind == "people" and item.get("last_name", "").lower() not in item.get("quote", "").lower():
                problems.append(f"people: quote does not contain surname {item.get('last_name')}")
    return problems


def write_answer(task_id: str, answer: dict, task_dir: Path = TASK_DIR, force: bool = False) -> list[str]:
    """Validates against the task's schema and quote rules, then writes <id>.response.json.
    Returns the problems found; nothing is written if there are any (unless force)."""
    task = json.loads((task_dir / f"{task_id}.task.json").read_text())
    problems = check_answer(task, answer)
    if problems and not force:
        return problems
    (task_dir / f"{task_id}.response.json").write_text(json.dumps(answer, indent=1))
    return problems
