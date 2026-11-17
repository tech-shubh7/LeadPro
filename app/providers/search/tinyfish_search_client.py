"""TinyFish TinySearch (api.search.tinyfish.ai): web and news search with a free allowance.

GET / ?query=&location=GB&language=en&domain_type=web|news&recency_minutes=  (X-API-Key header)
-> results[]: position, title, url, snippet, site_name, date (news and some web results).
No result count parameter: one call returns one page. 402 means the free allowance is used up.
"""

from datetime import date

import httpx

from app.config.settings import get_settings
from app.providers.search.serper_client import SearchHit
from app.utils.cost_tracker import CostTracker
from app.utils.http_retry import request_with_retry

BASE_URL = "https://api.search.tinyfish.ai"
_LOCATION = {"uk": "GB", "us": "US", "in": "IN"}
_RECENCY_MINUTES = {"qdr:d": 1440, "qdr:w": 10080, "qdr:m": 43200, "qdr:y": 525600}


class TinyFishSearchClient:
    def __init__(self, cost: CostTracker, client: httpx.AsyncClient | None = None):
        self.settings = get_settings()
        self.cost = cost
        self.client = client or httpx.AsyncClient(
            base_url=BASE_URL, timeout=30, headers={"X-API-Key": self.settings.tinyfish_api_key}
        )

    async def query(
        self, q: str, kind: str = "search", gl: str = "uk", num: int = 10, tbs: str | None = None
    ) -> list[SearchHit]:
        params = {
            "query": q,
            "location": _LOCATION.get(gl, gl.upper()),
            "language": "en",
            "domain_type": "news" if kind == "news" else "web",
        }
        if tbs in _RECENCY_MINUTES:
            params["recency_minutes"] = _RECENCY_MINUTES[tbs]
        async with self.cost.hold(0.0):  # inside the free allowance; recorded so usage shows on the run
            resp = await request_with_retry(self.client, "GET", "/", params=params)
            resp.raise_for_status()
            self.cost.record("tinyfish", f"search:{kind}", 1, 0.0)
        searched_on = date.today().isoformat()
        return [
            SearchHit(
                title=item.get("title", ""),
                link=item["url"],
                snippet=item.get("snippet", ""),
                date_text=item.get("date"),
                kind=kind,
                query=q,
                searched_on=searched_on,
            )
            for item in resp.json().get("results", [])[:num]
            if item.get("url")
        ]

    async def resolve_redirect(self, link: str) -> str:
        return link  # TinySearch returns the page URL itself

    async def aclose(self) -> None:
        await self.client.aclose()
