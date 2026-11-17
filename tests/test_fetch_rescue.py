"""The rendering fallback rescues technical failures only: never a site's refusal."""

import asyncio

import httpx
import pytest

from app.providers.web.page_fetcher import Fetcher, PageCache
from app.providers.web.tinyfish_fetch_client import RenderedPage

ARTICLE = (
    "<html><body><article>"
    + ("Acme Robotics is hiring a contract developer for its portal. " * 12)
    + "</article></body></html>"
)
JS_SHELL = '<html><head><script src="/app.js"></script></head><body><div id="root"></div></body></html>'


class FakeRescue:
    def __init__(self):
        self.calls = []

    async def render(self, url):
        self.calls.append(url)
        return RenderedPage(url, url, ARTICLE)

    async def aclose(self):
        return None


def _fetch(tmp_path, handler, robots=None, url="https://1.1.1.1/news/hiring"):
    rescue = FakeRescue()
    fetcher = Fetcher(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        cache=PageCache(tmp_path, None),
        rescue=rescue,
    )
    fetcher._robots["https://1.1.1.1"] = robots

    async def go():
        try:
            return await fetcher.fetch(url)
        finally:
            await fetcher.aclose()

    return asyncio.run(go()), rescue


def test_a_network_failure_is_rescued(tmp_path):
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    result, rescue = _fetch(tmp_path, handler)
    assert result.ok
    assert "contract developer" in result.text
    assert rescue.calls == ["https://1.1.1.1/news/hiring"]


def test_an_empty_javascript_shell_is_rescued(tmp_path):
    result, rescue = _fetch(
        tmp_path, lambda r: httpx.Response(200, text=JS_SHELL, headers={"content-type": "text/html"})
    )
    assert "contract developer" in result.text
    assert rescue.calls


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_site_refusal_is_never_routed_around(tmp_path, status):
    result, rescue = _fetch(
        tmp_path, lambda r: httpx.Response(status, text="no", headers={"content-type": "text/html"})
    )
    assert not result.ok
    assert rescue.calls == []


def test_robots_disallow_is_never_routed_around(tmp_path):
    from urllib.robotparser import RobotFileParser

    robots = RobotFileParser()
    robots.parse(["User-agent: *", "Disallow: /"])
    result, rescue = _fetch(tmp_path, lambda r: httpx.Response(200, text=ARTICLE), robots=robots)
    assert result.blocked_reason == "disallowed by robots.txt"
    assert rescue.calls == []


def test_a_good_page_is_not_sent_to_the_fallback(tmp_path):
    result, rescue = _fetch(
        tmp_path, lambda r: httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
    )
    assert result.ok
    assert rescue.calls == []
