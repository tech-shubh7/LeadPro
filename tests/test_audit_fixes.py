import asyncio

import httpx
import respx

from app.core.evidence_verify import quote_in_text
from app.core.fetcher import Fetcher, PageCache, extract_main_text
from app.core.normalize import extract_emails, is_role_email

PAGE = "Acme Ltd today confirmed it will open a second fulfilment centre in Leeds next spring, creating 40 jobs."


def test_ellipsis_quote_parts_must_all_appear_in_order():
    assert quote_in_text("Acme Ltd today confirmed ... fulfilment centre in Leeds", PAGE)
    assert quote_in_text("Acme Ltd today confirmed…creating 40 jobs", PAGE)
    assert not quote_in_text("creating 40 jobs ... Acme Ltd today confirmed", PAGE)  # wrong order
    assert not quote_in_text("Acme Ltd today confirmed ... a new office in Paris", PAGE)


def test_image_names_are_not_emails_and_role_list_extended():
    assert extract_emails("logo@2x.png jane@acme.co.uk", "acme.co.uk") == ["jane@acme.co.uk"]
    assert is_role_email("noreply@acme.co.uk") and is_role_email("press@acme.co.uk")


def test_team_page_names_survive_text_extraction():
    html = "<html><body><nav>Home</nav><div class='team'>" + "".join(
        f"<div class='card'><h3>Person {i} Surname{i}</h3><p>Head of Function {i}</p></div>" for i in range(12)
    ) + "</div></body></html>"
    text = extract_main_text(html)
    assert "Surname0" in text and "Surname11" in text


@respx.mock
def test_robots_403_and_5xx_mean_disallow(tmp_path):
    respx.get("https://locked.co.uk/robots.txt").respond(403)
    respx.get("https://flaky.co.uk/robots.txt").respond(503)
    page = respx.get("https://locked.co.uk/").respond(200, html="<p>x</p>")
    f = Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path, ttl=None))
    assert asyncio.run(f.fetch("https://locked.co.uk/")).blocked_reason == "disallowed by robots.txt"
    assert asyncio.run(f.fetch("https://flaky.co.uk/")).blocked_reason == "disallowed by robots.txt"
    assert page.call_count == 0


@respx.mock
def test_oversized_page_is_skipped(tmp_path):
    respx.get("https://huge.co.uk/robots.txt").respond(404)
    respx.get("https://huge.co.uk/").respond(200, html="x" * 6_000_000)
    f = Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path, ttl=None))
    assert asyncio.run(f.fetch("https://huge.co.uk/")).blocked_reason == "page too large"


def test_url_variants_still_verify():
    from app.core.evidence_verify import Claim, verify_claims
    pages = {"https://www.acme.co.uk/news/leeds/": PAGE}
    claims = [Claim("expansion", "https://acme.co.uk/news/leeds#top", "second fulfilment centre in Leeds")]
    verified, rejected = verify_claims(claims, pages)
    assert len(verified) == 1 and not rejected


@respx.mock
def test_retry_on_429_then_success():
    from app.core.http import request_with_retry
    route = respx.get("https://api.example.com/x")
    route.side_effect = [httpx.Response(429, headers={"retry-after": "0"}), httpx.Response(200, json={"ok": True})]
    resp = asyncio.run(request_with_retry(httpx.AsyncClient(), "GET", "https://api.example.com/x", base_delay=0))
    assert resp.status_code == 200 and route.call_count == 2
