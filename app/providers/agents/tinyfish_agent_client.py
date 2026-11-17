"""TinyFish TinyAgent: a browser agent that reads one page and returns structured rows.

POST https://agent.tinyfish.ai/v1/automation/run {url, goal, output_schema, agent_config: {max_steps,
max_duration_seconds}} (X-API-Key) -> {status: COMPLETED | FAILED, result, num_of_steps, error}.
Billed per step ($0.016 at list price), so every run carries a hard step cap reserved against the spend cap.
COMPLETED only means the browser ran: the rows are checked before use.
"""

from dataclasses import dataclass

import httpx

from app.config.settings import get_settings
from app.utils.cost_tracker import CostTracker
from app.utils.logger import get_logger

logger = get_logger(__name__)
BASE_URL = "https://agent.tinyfish.ai"
MAX_DURATION_S = 180

# The fields the agent must return. evidence_url and evidence_text exist so a lead can be verified on a
# page we fetch ourselves: the agent's word is never evidence.
COMPANY_ROWS_SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company_name": {"type": "string"},
                    "company_website": {"type": "string"},
                    "evidence_url": {"type": "string"},
                    "evidence_text": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["company_name", "evidence_url", "evidence_text"],
            },
        }
    },
    "required": ["companies"],
}
GUARDRAILS = (
    "Rules: read only what is publicly visible. Do not log in, sign up, accept paid offers or submit any "
    "form other than a search box. Copy evidence_text word for word from the page; never summarise it. "
    "Give evidence_url as the exact page where that text appears. Leave company_website empty unless the "
    "page shows it. Stop once you have {limit} companies."
)


@dataclass(frozen=True)
class BrowsedCompany:
    company_name: str
    company_website: str
    evidence_url: str
    evidence_text: str
    date: str


class TinyFishAgentClient:
    def __init__(self, cost: CostTracker, client: httpx.AsyncClient | None = None):
        self.settings = get_settings()
        self.cost = cost
        self.client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=MAX_DURATION_S + 30,
            headers={"X-API-Key": self.settings.tinyfish_api_key},
        )

    def _inr(self, steps: int) -> float:
        return steps * self.settings.tinyfish_usd_per_agent_step * self.settings.usd_to_inr

    async def list_companies(self, url: str, goal: str, max_steps: int, limit: int) -> list[BrowsedCompany]:
        payload = {
            "url": url,
            "goal": f"{goal}\n\n{GUARDRAILS.format(limit=limit)}",
            "output_schema": COMPANY_ROWS_SCHEMA,
            "browser_profile": "lite",
            "agent_config": {"max_steps": max_steps, "max_duration_seconds": MAX_DURATION_S},
        }
        async with self.cost.hold(self._inr(max_steps)):  # the most this run can cost
            resp = await self.client.post("/v1/automation/run", json=payload)
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            steps = body.get("num_of_steps") or (max_steps if resp.status_code == 200 else 0)
            if steps:
                self.cost.record("tinyfish", "agent", steps, self._inr(steps))
        if resp.status_code != 200 or body.get("status") != "COMPLETED":
            error = (body.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
            logger.warning("tinyfish agent could not read %s: %s", url, error)
            return []
        rows = ((body.get("result") or {}).get("companies")) or []
        return [
            _row(r)
            for r in rows[:limit]
            if isinstance(r, dict) and r.get("company_name") and r.get("evidence_url")
        ]

    async def aclose(self) -> None:
        await self.client.aclose()


def _row(r: dict) -> BrowsedCompany:
    return BrowsedCompany(
        str(r.get("company_name", "")).strip(),
        str(r.get("company_website") or "").strip(),
        str(r.get("evidence_url", "")).strip(),
        str(r.get("evidence_text") or "").strip(),
        str(r.get("date") or "").strip(),
    )
