"""Event date vs publication date, and one signal per real-world event."""

import asyncio
from datetime import date

from app.models.enums import DateSource
from app.providers.web.page_fetcher import FetchResult
from app.schemas.job_spec_schema import JobSpec
from app.services.extraction_service import (
    ExtractOut,
    QuotedSignal,
    VerifiedSignal,
    collapse_duplicate_events,
    extract,
)
from app.services.research_service import ResearchBundle
from app.services.scoring_service import fresh_signals
from app.utils.dates import TrustedDate, parse_event_date
from app.utils.evidence_verifier import phrase_near_quote

TODAY = date(2026, 10, 30)
PUBLISHED = TrustedDate(date(2026, 10, 20), DateSource.ARTICLE_META)


def test_event_date_phrases_parse_to_the_end_of_the_stated_period():
    assert parse_event_date("opened in March 2025") == date(2025, 3, 31)
    assert parse_event_date("on 3 June 2026") == date(2026, 6, 3)
    assert parse_event_date("June 3rd, 2026") == date(2026, 6, 3)
    assert parse_event_date("Sept. 2025") == date(2025, 9, 30)
    assert parse_event_date("back in 2024") == date(2024, 12, 31)
    assert parse_event_date("on 2025-11-04") == date(2025, 11, 4)
    assert parse_event_date("announced last week") is None
    assert parse_event_date("31 February 2026") is None


def test_date_phrase_must_be_near_the_signal_quote():
    page = (
        "Acme opened its Leeds warehouse in March 2025 to serve the north."
        + " filler text" * 200
        + " Founded in 2009."
    )
    assert phrase_near_quote("in March 2025", "Acme opened its Leeds warehouse", page)
    assert not phrase_near_quote("in 2009", "Acme opened its Leeds warehouse", page)  # far away on the page
    assert not phrase_near_quote("in April 2025", "Acme opened its Leeds warehouse", page)  # not on the page


def _sig(
    url="https://news.example/a",
    published=PUBLISHED,
    event=None,
    key="",
    claim="Acme opened a Leeds warehouse",
):
    return VerifiedSignal(
        "expansion",
        claim,
        url,
        "Acme opened its new Leeds warehouse",
        published,
        event_date=event,
        event_date_quote="in March 2025" if event else "",
        event_key=key,
    )


def test_old_event_in_a_new_article_is_an_old_signal():
    s = _sig(event=date(2025, 3, 31))
    assert s.effective_date == date(2025, 3, 31)
    assert "event stated as 'in March 2025'" in s.date_basis()


def test_stated_date_never_makes_a_signal_fresher():
    assert (
        _sig(event=date(2026, 12, 31)).effective_date == PUBLISHED.value
    )  # future/upcoming: publication date
    assert (
        _sig(published=None, event=date(2026, 10, 1)).effective_date is None
    )  # undated source stays undated


def test_republished_reports_of_one_event_count_once_with_the_earliest_date():
    original = _sig(
        "https://a.example/1", TrustedDate(date(2026, 3, 1), DateSource.ARTICLE_META), key="leeds-warehouse"
    )
    reprint = _sig("https://b.example/2", PUBLISHED, key="Leeds-Warehouse ")
    other = _sig("https://c.example/3", PUBLISHED, key="series-a", claim="Acme raised a Series A")
    merged = collapse_duplicate_events([reprint, original, other])
    assert len(merged) == 2
    head = next(m for m in merged if m.event_key.lower().strip() == "leeds-warehouse")
    assert head.source_url == "https://a.example/1"
    assert head.corroborating_urls == ["https://b.example/2"]
    assert head.effective_date == date(2026, 3, 1)  # the reprint's newer date doesn't make the event fresh


def test_identical_claims_without_a_key_are_merged():
    merged = collapse_duplicate_events([_sig("https://a.example/1"), _sig("https://b.example/2")])
    assert len(merged) == 1
    assert merged[0].corroborating_urls == ["https://b.example/2"]


ARTICLE_URL = "https://acme.co.uk/news/2026/10/update"
ARTICLE_TEXT = (
    "Acme Goods opened its new Leeds fulfilment hub in March 2025 and is now hiring. "
    "We sell quality consumer products online to customers across the United Kingdom."
)


class _LLM:
    # the pipeline asks the provider which model each step wants
    extract_model = "fake-extract"
    judge_model = "fake-judge"

    def __init__(self, date_phrase: str):
        self.date_phrase = date_phrase

    async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
        assert schema is ExtractOut
        return ExtractOut(
            sources_match_target=True,
            company_name="Acme",
            company_description="Online brand",
            is_recruitment_or_staffing_firm=False,
            facts=[],
            people=[],
            signals=[
                QuotedSignal(
                    signal_type="expansion",
                    claim="Opened Leeds hub",
                    source_url=ARTICLE_URL,
                    quote="Acme Goods opened its new Leeds fulfilment hub",
                    event_date_quote=self.date_phrase,
                    event_key="leeds-hub",
                )
            ],
        )


def _run_extract(date_phrase: str):
    html = f'<html><head><meta property="article:published_time" content="2026-10-20"></head><body>{ARTICLE_TEXT}</body></html>'
    page = FetchResult(ARTICLE_URL, ARTICLE_URL, 200, html, ARTICLE_TEXT, "2026-10-30T00:00:00")
    bundle = ResearchBundle(domain="acme.co.uk", homepage=None, pages={ARTICLE_URL: page})
    spec = JobSpec.model_validate(
        {
            "client": {"key": "k", "name": "n"},
            "ask": "a",
            "count": 1,
            "icp": {},
            "playbook": {
                "signals": [{"type": "expansion", "description": "d", "queries": []}],
                "buyer_titles": ["COO"],
            },
        }
    )
    return asyncio.run(extract(bundle, spec, _LLM(date_phrase), TODAY))


def test_extraction_uses_the_stated_event_date_for_freshness():
    ext = _run_extract("in March 2025")
    assert ext.signals[0].trusted_date.value == date(2026, 10, 20)  # article is new...
    assert ext.signals[0].effective_date == date(2025, 3, 31)  # ...but the event is 19 months old
    assert fresh_signals(ext, TODAY, 90) == []


def test_invented_event_date_phrase_is_ignored():
    ext = _run_extract("in September 2026")  # not in the source text: the model can't move the date
    assert ext.signals[0].event_date is None
    assert ext.signals[0].effective_date == date(2026, 10, 20)
