"""Serper.dev Google search. Endpoints/sections per a public client (langchain google_serper):
POST https://google.serper.dev/{search|news} with X-API-KEY; results under "organic" / "news".
Per-item fields used: title, link, snippet. `date` is read when present (unverified, F1);
signals never rely on it alone: the article page date is preferred when the page is fetched.
"""
from dataclasses import dataclass

import asyncio
import hashlib
import json
from pathlib import Path

import httpx

from app.core.http import request_with_retry

from app.config import get_settings
from app.core.cost import CostTracker

BASE_URL = "https://google.serper.dev"
GOOGLE_REDIRECT = "https://www.google.com/goto?"
_SECTION = {"search": "organic", "news": "news"}


@dataclass(frozen=True)
class SearchHit:
    title: str
    link: str
    snippet: str
    date_text: str | None
    kind: str  # "search" | "news"
    query: str


class SerperClient:
    def __init__(self, cost: CostTracker, client: httpx.AsyncClient | None = None):
        self.settings = get_settings()
        self.cost = cost
        self.client = client or httpx.AsyncClient(timeout=30)
        self._redirect_sem = asyncio.Semaphore(2)

    def _cache_path(self, body: dict, kind: str) -> Path:
        key = hashlib.sha256(json.dumps([kind, body], sort_keys=True).encode()).hexdigest()[:24]
        return Path("cache/serper") / f"{key}.json"

    async def query(self, q: str, kind: str = "search", gl: str = "uk", num: int = 10, tbs: str | None = None) -> list[SearchHit]:
        body = {"q": q, "gl": gl, "num": num} | ({"tbs": tbs} if tbs else {})
        cache = self._cache_path(body, kind) if self.settings.is_dev else None
        if cache and cache.exists():  # dev only: identical query already paid for
            return [SearchHit(**h) for h in json.loads(cache.read_text())]
        hits = await self._query_live(q, body, kind, num)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps([vars(h) for h in hits]))
        return hits

    async def _query_live(self, q: str, body: dict, kind: str, num: int) -> list[SearchHit]:
        credits = 1 if num <= 10 else 2
        price = credits * self.settings.serper_usd_per_credit * self.settings.usd_to_inr
        self.cost.ensure_budget(price)
        resp = await request_with_retry(self.client, "POST", f"{BASE_URL}/{kind}", json=body,
                                        headers={"X-API-KEY": self.settings.serper_api_key})
        resp.raise_for_status()
        used = resp.json().get("credits", credits)  # Serper reports credits used per request
        self.cost.record("serper", kind, used, used * self.settings.serper_usd_per_credit * self.settings.usd_to_inr)
        hits = [
            SearchHit(
                title=item.get("title", ""),
                link=item.get("link", ""),
                snippet=item.get("snippet", ""),
                date_text=item.get("date"),
                kind=kind,
                query=q,
            )
            for item in resp.json().get(_SECTION[kind], [])
            if item.get("link")
        ]
        return hits

    async def resolve_redirect(self, link: str) -> str:
        """Search items can be google.com/goto links; the 302 Location is the real page.
        Resolved only for links actually used as evidence, two at a time, with one retry."""
        if not link.startswith(GOOGLE_REDIRECT):
            return link
        async with self._redirect_sem:
            for attempt in range(2):
                try:
                    resp = await self.client.get(link, follow_redirects=False)
                    if location := resp.headers.get("location"):
                        return location
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1.5 * (attempt + 1))
        return link
