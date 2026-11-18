"""Google redirect links in search results are replaced by the real page before they become evidence.

Found in job 26 (2026-11-18): only /goto? links were recognised, but most redirect links are /url? links
(232 of 319 in the search cache), so a signal's source could be an opaque google.com link instead of the
Glassdoor ad it came from. Fully offline (respx)."""

import asyncio

import httpx
import pytest
import respx

from app.providers.search.serper_client import SearchHit, SerperClient, _real_page, is_search_redirect
from app.services.research_service import company_news
from app.utils.cost_tracker import CostTracker

URL_FORM = "https://www.google.com/url?opi=89978449&q=CAES1QEB&sa=U"
GOTO_FORM = "https://www.google.com/goto?q=abc"
REAL = "https://www.glassdoor.co.uk/job-listing/developpeur-web-brome-JV_IC3935929.htm?jl=1010128156761"


def test_both_google_redirect_forms_are_recognised():
    assert is_search_redirect(URL_FORM)
    assert is_search_redirect(GOTO_FORM)
    assert not is_search_redirect(REAL)
    assert not is_search_redirect("https://www.google.com/search?q=x")  # a search page, not a redirect


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (REAL, REAL),
        ("https://consent.google.com/ml?continue=x", None),  # Google's own pages are not the source
        ("https://www.google.com/sorry/index", None),
        ("/url?q=relative", None),
        ("javascript:alert(1)", None),
        (None, None),
    ],
)
def test_only_a_real_web_page_is_accepted_as_the_target(location, expected):
    assert _real_page(location) == expected


def _serper() -> SerperClient:
    return SerperClient(CostTracker(cap_inr=5), client=httpx.AsyncClient())


@respx.mock
def test_a_url_form_redirect_resolves_to_the_real_page():
    respx.get(URL_FORM).respond(302, headers={"location": REAL})

    async def go():
        serper = _serper()
        try:
            return await serper.resolve_redirect(URL_FORM)
        finally:
            await serper.aclose()

    assert asyncio.run(go()) == REAL


@respx.mock
def test_a_redirect_into_a_google_page_keeps_the_original_link(monkeypatch):
    respx.get(URL_FORM).respond(302, headers={"location": "https://consent.google.com/ml"})

    async def no_wait(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)

    async def go():
        serper = _serper()
        try:
            return await serper.resolve_redirect(URL_FORM)
        finally:
            await serper.aclose()

    assert asyncio.run(go()) == URL_FORM  # still opens in a browser; never replaced by a consent page


class NewsSerper:
    async def query(self, q, kind="search", gl="uk", num=10, tbs=None):
        return [
            SearchHit(
                "BROME hiring Développeur Web",
                URL_FORM,
                "BROME Consulting freelance role",
                "1 day ago",
                "news",
                q,
            ),
            SearchHit("Unrelated", "https://example.com/x", "nothing about them", None, "news", q),
        ]

    async def resolve_redirect(self, link):
        return REAL if link == URL_FORM else link


def test_company_news_hits_carry_the_real_page_link():
    hits = asyncio.run(company_news(NewsSerper(), "BROME", "us", 1))
    assert [h.link for h in hits] == [REAL]  # the unrelated hit is dropped, the redirect resolved
