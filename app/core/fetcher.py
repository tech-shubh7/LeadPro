"""Live page fetching: robots.txt respected, polite per-host pacing, short-lived cache.

The cache exists to avoid paying/fetching twice inside a run, not to serve stale facts:
production TTL is 24h; in dev it never expires so debugging reruns are free.
"""
import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from lxml import html as lxml_html

from app.config import get_settings
from app.core.netguard import guarded_client

USER_AGENT = "LeadProResearchBot/0.1 (B2B research; respects robots.txt)"
MIN_HOST_INTERVAL_S = 1.0
MAX_PAGE_BYTES = 5_000_000
MIN_GOOD_TEXT_CHARS = 400


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    html: str
    text: str
    fetched_at: str  # ISO timestamp
    from_cache: bool = False
    blocked_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.blocked_reason is None and 200 <= self.status < 300


def visible_text(page_html: str) -> str:
    """All visible text including header/footer (where company numbers, phones and addresses live)."""
    try:
        tree = lxml_html.fromstring(page_html)
    except (ValueError, lxml_html.etree.ParserError):
        return ""
    for bad in tree.xpath("//script|//style|//noscript|//template"):
        bad.drop_tree()
    return " ".join(tree.text_content().split())


def extract_main_text(page_html: str) -> str:
    """Main text of a page. Precision mode first; team/contact pages often lose names in
    precision mode, so fall back to recall mode, then to the plain body text."""
    text = trafilatura.extract(page_html, include_comments=False, include_tables=True, favor_precision=True) or ""
    if len(text) >= MIN_GOOD_TEXT_CHARS:
        return text
    recall = trafilatura.extract(page_html, include_comments=False, include_tables=True, favor_recall=True) or ""
    if len(recall) >= MIN_GOOD_TEXT_CHARS:
        return recall
    return max((text, recall, visible_text(page_html)), key=len)


class PageCache:
    def __init__(self, root: Path, ttl: timedelta | None):
        self.root = root
        self.ttl = ttl
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        return self.root / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def get(self, url: str) -> FetchResult | None:
        path = self._path(url)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if self.ttl is not None and datetime.now(UTC) - datetime.fromisoformat(data["fetched_at"]) > self.ttl:
            return None
        data["from_cache"] = True
        return FetchResult(**data)

    def prune(self, max_age: timedelta) -> int:
        """Deletes cached pages older than `max_age`. Cached pages include team pages, which name people:
        they are kept only as long as they are useful, not indefinitely."""
        cutoff = time.time() - max_age.total_seconds()
        removed = 0
        for path in self.root.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                pass
        return removed

    def put(self, result: FetchResult) -> None:
        self._path(result.url).write_text(json.dumps(asdict(result) | {"from_cache": False}))


class Fetcher:
    def __init__(self, client: httpx.AsyncClient | None = None, cache: PageCache | None = None):
        settings = get_settings()
        self.client = client or guarded_client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.8"}, follow_redirects=True, timeout=20
        )
        ttl = None if settings.is_dev else timedelta(hours=settings.page_cache_ttl_hours)
        self.cache = cache or PageCache(Path("cache/pages"), ttl)
        self._robots: dict[str, RobotFileParser | None | bool] = {}
        self._last_hit: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def _allowed(self, url: str) -> bool:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_locks.setdefault(origin, asyncio.Lock()):
            if origin not in self._robots:
                self._robots[origin] = await self._load_robots(origin)
        parser = self._robots[origin]
        if parser is False:
            return False
        return parser is None or parser.can_fetch(USER_AGENT, url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None | bool:
        """None = no rules (allow all); False = disallow all (401/403/5xx, as RFC 9309 advises)."""
        try:
            resp = await self.client.get(f"{origin}/robots.txt")
        except httpx.HTTPError:
            return None
        if resp.status_code in (401, 403) or resp.status_code >= 500:
            return False
        if resp.status_code >= 400:
            return None
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser

    async def _pace(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = MIN_HOST_INTERVAL_S - (time.monotonic() - self._last_hit.get(host, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[host] = time.monotonic()

    async def fetch(self, url: str) -> FetchResult:
        if cached := self.cache.get(url):
            return cached
        now = datetime.now(UTC).isoformat()
        if not await self._allowed(url):
            return FetchResult(url, url, 0, "", "", now, blocked_reason="disallowed by robots.txt")
        await self._pace(urlparse(url).netloc)
        try:
            async with self.client.stream("GET", url) as resp:
                final_url = str(resp.url)
                is_html = "html" in resp.headers.get("content-type", "")
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_PAGE_BYTES:
                    return FetchResult(url, final_url, resp.status_code, "", "", now, blocked_reason="page too large")
                if not is_html:  # PDFs, images, video: never read, so never downloaded
                    return FetchResult(url, final_url, resp.status_code, "", "", now)
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:  # no or false Content-Length: stop reading, don't buffer it all
                        return FetchResult(url, final_url, resp.status_code, "", "", now, blocked_reason="page too large")
                encoding = resp.charset_encoding or "utf-8"
        except httpx.HTTPError as exc:
            return FetchResult(url, url, 0, "", "", now, blocked_reason=f"fetch error: {type(exc).__name__}")
        try:
            page_html = bytes(body).decode(encoding, errors="replace")
        except LookupError:  # a charset name Python doesn't know (e.g. 'utf8mb4')
            page_html = bytes(body).decode("utf-8", errors="replace")
        # trafilatura/lxml are CPU-bound: run them off the event loop so concurrent fetches keep going.
        text = await asyncio.to_thread(extract_main_text, page_html)
        result = FetchResult(url, final_url, resp.status_code, page_html, text, now)
        if result.ok:
            self.cache.put(result)
        return result

    async def aclose(self) -> None:
        await self.client.aclose()
