"""Re-check before delivery: a closed opportunity is held back; an uncertain check never removes a lead."""

import asyncio
from dataclasses import dataclass, field
from datetime import date

from app.models.enums import DateSource
from app.providers.web.page_fetcher import FetchResult
from app.services.extraction_service import Extraction, VerifiedSignal
from app.services.revalidation_service import revalidate
from app.utils.dates import TrustedDate

TODAY = date(2026, 11, 17)
QUOTE = "we are looking for an agency to rebuild our website"


def _signal(url, source_class="company_site"):
    s = VerifiedSignal("asks", "claim", url, QUOTE, TrustedDate(date(2026, 11, 10), DateSource.ARTICLE_META))
    s.intent, s.source_class, s.date_confidence = "asks_for_supplier", source_class, "high"
    return s


@dataclass
class Lead:
    extraction: Extraction
    people: list = field(default_factory=list)


def _lead(*signals) -> Lead:
    return Lead(Extraction("Acme", "d", False, signals=list(signals)))


class Fetcher:
    def __init__(self, pages):  # url -> (status, text, blocked_reason)
        self.pages, self.fresh_calls = pages, []

    async def fetch(self, url, fresh=False):
        self.fresh_calls.append(fresh)
        status, text, blocked = self.pages[url]
        return FetchResult(url, url, status, "", text, "now", blocked_reason=blocked)


def _run(leads, pages):
    fetcher = Fetcher(pages)
    return asyncio.run(revalidate(leads, fetcher, TODAY, 90)), fetcher


def test_a_closed_posting_is_held_back():
    dropped, fetcher = _run(
        [_lead(_signal("https://acme.co.uk/brief"))], {"https://acme.co.uk/brief": (404, "", None)}
    )
    assert dropped == {0: "buying sign no longer live at delivery (posting or page removed)"}
    assert fetcher.fresh_calls == [True]  # never the cached copy


def test_a_quote_that_left_the_page_is_held_back():
    dropped, _ = _run(
        [_lead(_signal("https://acme.co.uk/brief"))],
        {"https://acme.co.uk/brief": (200, "This brief has now closed.", None)},
    )
    assert "quote no longer on the page" in dropped[0]


def test_a_live_page_keeps_the_lead():
    dropped, _ = _run(
        [_lead(_signal("https://acme.co.uk/brief"))],
        {"https://acme.co.uk/brief": (200, f"Hello. {QUOTE}. Thanks.", None)},
    )
    assert dropped == {}


def test_an_uncertain_check_never_removes_a_lead():
    dropped, _ = _run(
        [_lead(_signal("https://acme.co.uk/brief"))],
        {"https://acme.co.uk/brief": (0, "", "fetch error: ConnectTimeout")},
    )
    assert dropped == {}


def test_one_live_sign_is_enough_to_keep_the_lead():
    gone, live = _signal("https://acme.co.uk/a"), _signal("https://acme.co.uk/b")
    dropped, _ = _run(
        [_lead(gone, live)],
        {"https://acme.co.uk/a": (404, "", None), "https://acme.co.uk/b": (200, QUOTE, None)},
    )
    assert dropped == {}


def test_an_award_notice_is_not_rechecked():
    dropped, fetcher = _run(
        [_lead(_signal("https://www.contractsfinder.service.gov.uk/Notice/x", "official_notice"))], {}
    )
    assert dropped == {}
    assert fetcher.fresh_calls == []


def test_a_lead_with_no_buying_sign_is_not_rechecked():
    dropped, fetcher = _run([_lead()], {})
    assert dropped == {}
    assert fetcher.fresh_calls == []
