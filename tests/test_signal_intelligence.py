"""Buying-intent judgement: a real quote is not automatically a real buying opportunity."""

import asyncio
from datetime import date

import pytest

from app.models.enums import DateSource
from app.schemas.job_spec_schema import JobSpec
from app.services.extraction_service import VerifiedSignal
from app.services.signal_intelligence_service import (
    IntentOut,
    SignalIntent,
    assess_signals,
    can_qualify,
    date_confidence,
    source_class,
)
from app.utils.dates import TrustedDate

SPEC = JobSpec.model_validate(
    {
        "client": {"key": "k", "name": "Brightpath Web"},
        "ask": "UK firms needing a website",
        "count": 5,
        "icp": {},
        "playbook": {"signals": [], "buyer_titles": ["founder"]},
    }
)


def _signal(quote, source=DateSource.ARTICLE_META, url="https://acme.co.uk/news/1"):
    return VerifiedSignal("hiring", "claim", url, quote, TrustedDate(date(2026, 11, 1), source))


@pytest.mark.parametrize(
    ("url", "kinds", "expected"),
    [
        ("https://www.contractsfinder.service.gov.uk/Notice/x", {}, "official_notice"),
        ("https://jobs.lever.co/acme/1", {"https://jobs.lever.co/acme/1": "ats"}, "job_posting"),
        ("https://acme.co.uk/careers/dev", {}, "job_posting"),
        ("https://acme.co.uk/news/launch", {}, "company_site"),
        ("https://acme.co.uk/brief.pdf", {}, "company_document"),
        ("https://other.org/brief.pdf", {}, "document"),
        ("https://press.example/acme", {"https://press.example/acme": "news"}, "news"),
        ("https://blog.example/acme", {}, "other"),
    ],
)
def test_where_a_quote_came_from_is_classified_by_rules(url, kinds, expected):
    assert source_class(url, "acme.co.uk", kinds) == expected


def test_a_date_taken_only_from_the_url_is_low_confidence_and_cannot_qualify_alone():
    signal = _signal("we are launching", DateSource.URL_PATH)
    signal.date_confidence = date_confidence(signal)
    assert signal.date_confidence == "low"
    assert not can_qualify(signal)
    signal.corroborating_urls = ["https://second-source.example/x"]
    assert date_confidence(signal) == "medium"  # backed by another report of the same event


class IntentLLM:
    extract_model = judge_model = "m"

    def __init__(self, intents, fail=False):
        self.intents, self.fail = intents, fail

    async def parse(self, model, instructions, data, schema, **kw):
        if self.fail:
            raise RuntimeError("model down")
        assert schema is IntentOut
        return IntentOut(
            items=[SignalIntent(signal_index=i, intent=v, reason="r") for i, v in enumerate(self.intents)]
        )


def test_in_house_hiring_counts_against_buying_not_for_it():
    permanent = _signal("hiring a permanent full-stack engineer for our in-house team")
    contract = _signal("seeking a contract developer for a 3-month rebuild")
    result = asyncio.run(
        assess_signals(
            [permanent, contract],
            SPEC,
            "Acme",
            "acme.co.uk",
            {},
            IntentLLM(["in_house_build", "outsourcing_signal"]),
        )
    )
    assert result.against == [permanent]
    assert result.buying == [contract]
    assert not can_qualify(permanent)
    assert can_qualify(contract)


def test_a_failed_intent_check_keeps_every_signal_as_before():
    signal = _signal("seeking a contract developer")
    result = asyncio.run(assess_signals([signal], SPEC, "Acme", "acme.co.uk", {}, IntentLLM([], fail=True)))
    assert result.buying == [signal]
    assert signal.intent == ""
    assert can_qualify(signal)


def test_the_scorer_sees_evidence_against_separately():
    from app.services.extraction_service import Extraction
    from app.services.scoring_service import evidence_listing

    against = _signal("we build everything in-house")
    against.intent, against.intent_reason = "in_house_build", "builds in-house"
    ext = Extraction("Acme", "d", False, against=[against])
    listing, ids = evidence_listing(ext, date(2026, 11, 17), 90)
    assert "[X1] AGAINST (in_house_build" in listing
    assert "X1" in ids


# --- combining a company's buying signs -------------------------------------------------------------

from app.services.signal_intelligence_service import TIER_GOOD, TIER_STRONG, TIER_WEAK, combine  # noqa: E402


def _classified(intent, url="https://acme.co.uk/news/1"):
    s = _signal("q", url=url)
    s.intent, s.date_confidence = intent, "high"
    return s


def test_asking_for_a_supplier_is_strong_on_its_own():
    assert combine([_classified("asks_for_supplier")]).tier == TIER_STRONG


def test_two_different_kinds_of_demand_are_strong():
    stack = combine(
        [
            _classified("outsourcing_signal"),
            _classified("project_needs_offer", "https://www.insidermedia.com/news/a"),
        ]
    )
    assert stack.tier == TIER_STRONG
    assert stack.sources == 2
    assert stack.summary == "Looking for outside help + Project needs this work · 2 signs, 2 sources"


def test_one_demand_sign_is_good_and_budget_alone_is_weak():
    assert combine([_classified("outsourcing_signal")]).tier == TIER_GOOD
    assert (
        combine(
            [_classified("budget_only"), _classified("budget_only", "https://www.bdaily.co.uk/articles/b")]
        ).tier
        == TIER_WEAK
    )


def test_an_unclassified_sign_counts_as_a_single_real_sign():
    """When the intent check is unavailable, nothing gets better or worse than before it existed."""
    assert combine([_classified("")]).tier == TIER_GOOD


def test_no_qualifying_sign_has_no_tier():
    assert combine([]).tier == ""


def test_strong_leads_rank_above_higher_scoring_weak_ones():
    from types import SimpleNamespace

    from app.services.export_service import rank_key

    weak = SimpleNamespace(stack=combine([_classified("budget_only")]), score=SimpleNamespace(total=90))
    strong = SimpleNamespace(
        stack=combine([_classified("asks_for_supplier")]), score=SimpleNamespace(total=72)
    )
    assert sorted([weak, strong], key=rank_key) == [strong, weak]
