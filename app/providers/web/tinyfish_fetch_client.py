"""TinyFish TinyFetch (api.fetch.tinyfish.ai): renders a page in a real browser and returns its HTML.

POST / {urls: [..<=10], format: "html", ttl} (X-API-Key) -> results[]: url, final_url, title, text,
published_date; errors[]: url, error (bot_blocked, login_required, page_not_found, timeout, ...).
Used only to rescue pages our own fetcher could not read for technical reasons (see Fetcher.fetch).
"""

from dataclasses import dataclass

import httpx

from app.config.settings import get_settings
from app.utils.cost_tracker import CostTracker
from app.utils.http_retry import request_with_retry
from app.utils.logger import get_logger

logger = get_logger(__name__)
BASE_URL = "https://api.fetch.tinyfish.ai"


@dataclass(frozen=True)
class RenderedPage:
    url: str
    final_url: str
    html: str


class TinyFishFetchClient:
    def __init__(self, cost: CostTracker | None = None, client: httpx.AsyncClient | None = None):
        self.settings = get_settings()
        self.cost = cost
        self.client = client or httpx.AsyncClient(
            base_url=BASE_URL, timeout=120, headers={"X-API-Key": self.settings.tinyfish_api_key}
        )

    async def render(self, url: str) -> RenderedPage | None:
        """The rendered HTML, or None when TinyFish could not get it (blocked, login wall, gone)."""
        resp = await request_with_retry(
            self.client,
            "POST",
            "/",
            json={"urls": [url], "format": "html", "ttl": 0, "per_url_timeout_ms": 60000},
        )
        if resp.status_code != 200:
            logger.warning("tinyfish fetch refused %s: HTTP %s", url, resp.status_code)
            return None
        body = resp.json()
        for error in body.get("errors", []):
            logger.info("tinyfish could not render %s: %s", url, error.get("error"))
        result = next(
            (r for r in body.get("results", []) if isinstance(r.get("text"), str) and r["text"]), None
        )
        if result is None:
            return None
        if self.cost is not None:
            self.cost.record("tinyfish", "fetch", 1, 0.0)  # inside the free daily allowance
        return RenderedPage(result["url"], result.get("final_url") or result["url"], result["text"])

    async def aclose(self) -> None:
        await self.client.aclose()
