import asyncio

import httpx
import respx

from app.core.fetcher import Fetcher, PageCache

ARTICLE = "<html><body><article><h1>Acme opens Leeds site</h1><p>" + "Acme Ltd announced a new warehouse in Leeds today. " * 5 + "</p></article></body></html>"


def _fetcher(tmp_path):
    return Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path, ttl=None))


@respx.mock
def test_fetch_extracts_text_and_caches(tmp_path):
    respx.get("https://acme.co.uk/robots.txt").respond(404)
    route = respx.get("https://acme.co.uk/news/leeds").respond(200, html=ARTICLE)
    f = _fetcher(tmp_path)
    first = asyncio.run(f.fetch("https://acme.co.uk/news/leeds"))
    second = asyncio.run(f.fetch("https://acme.co.uk/news/leeds"))
    assert first.ok and "new warehouse in Leeds" in first.text
    assert second.from_cache and route.call_count == 1


@respx.mock
def test_robots_disallow_is_respected(tmp_path):
    respx.get("https://shy.co.uk/robots.txt").respond(200, text="User-agent: *\nDisallow: /private")
    route = respx.get("https://shy.co.uk/private/team").respond(200, html=ARTICLE)
    result = asyncio.run(_fetcher(tmp_path).fetch("https://shy.co.uk/private/team"))
    assert not result.ok and result.blocked_reason == "disallowed by robots.txt"
    assert route.call_count == 0


@respx.mock
def test_errors_are_not_cached(tmp_path):
    respx.get("https://down.co.uk/robots.txt").respond(404)
    respx.get("https://down.co.uk/").respond(503)
    f = _fetcher(tmp_path)
    assert not asyncio.run(f.fetch("https://down.co.uk/")).ok
    assert f.cache.get("https://down.co.uk/") is None
