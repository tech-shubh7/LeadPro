"""Searches ask Google for the run's own window (2026-11-19: a "today or yesterday" run searched the past
month, paying for results the date checks then discarded). Offline."""

import asyncio

import pytest

from app.services.research_service import company_news
from app.utils.dates import search_window


@pytest.mark.parametrize(
    ("days", "tbs"), [(1, "qdr:d"), (2, "qdr:w"), (7, "qdr:w"), (25, "qdr:m"), (60, "qdr:y")]
)
def test_the_narrowest_google_window_that_still_covers_the_run(days, tbs):
    assert search_window(days) == tbs


class Recorder:
    def __init__(self):
        self.tbs = []

    async def query(self, q, kind="search", gl="uk", num=10, tbs=None):
        self.tbs.append(tbs)
        return []

    async def resolve_redirect(self, link):
        return link


def test_company_news_uses_the_window_it_is_given():
    serper = Recorder()
    asyncio.run(company_news(serper, "Acme", "us", 1, search_window(2)))
    assert serper.tbs == ["qdr:w"]


def test_discovery_searches_use_the_run_window():
    """The discovery loop builds its time filter from the run's freshness window, not a fixed month."""
    import inspect

    from app.services import discovery_service

    source = inspect.getsource(discovery_service._discover)
    assert "search_window(spec.freshness_days)" in source
    assert '"qdr:m" if' not in source
