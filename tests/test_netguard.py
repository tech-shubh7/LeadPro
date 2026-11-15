"""SSRF guard: the console fetches URLs typed into a form and found on the web."""
import asyncio

import httpx
import pytest

from app.core import netguard
from app.core.fetcher import Fetcher, PageCache


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/",
    "http://192.168.1.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/",
    "https://example.com:5432/", "ftp://example.com/",
])
def test_internal_destinations_are_refused(url):
    with pytest.raises(httpx.RequestError):
        asyncio.run(netguard.check_url(httpx.URL(url)))


def test_a_public_literal_address_is_allowed():
    asyncio.run(netguard.check_url(httpx.URL("https://1.1.1.1/")))


def test_a_redirect_into_the_network_is_not_followed(tmp_path):
    """Each hop is checked, so a public page cannot bounce the fetcher to the metadata endpoint."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "1.1.1.1":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="<html>secret</html>", headers={"content-type": "text/html"})

    client = netguard.guarded_client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher(client=client, cache=PageCache(tmp_path, None))
    fetcher._robots["https://1.1.1.1"] = None  # no robots lookup in this test

    async def go():
        try:
            return await fetcher.fetch("https://1.1.1.1/")
        finally:
            await fetcher.aclose()

    result = asyncio.run(go())
    assert not result.ok and "secret" not in result.html
