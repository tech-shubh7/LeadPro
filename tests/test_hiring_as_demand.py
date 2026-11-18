"""Capacity sellers (white-label / outsourced development): a target hiring for the work the client supplies
is demand, not evidence it builds in-house. Found in job 27 (2026-11-18): every agency on LinkedIn hiring a
developer was rejected as 'building it in-house', for a client whose pitch is 'our team instead of a hire'.
Fully offline."""

import asyncio
from datetime import date

import pytest

from app.models.enums import DateSource
from app.services.extraction_service import VerifiedSignal
from app.services.intake_service import fallback_plan, plan_from_job, plan_to_json, sells_capacity
from app.services.scoring_service import CAPACITY_SELLER_RULE, ScoreOut, score
from app.services.signal_intelligence_service import (
    HIRING_FOR_OFFER,
    INTENT_LABELS,
    TIER_GOOD,
    TIER_STRONG,
    assess_signals,
    can_qualify,
    combine,
)
from app.utils.dates import TrustedDate
from tests.test_signal_intelligence import SPEC, IntentLLM

POSTING = "https://www.linkedin.com/jobs/view/4491589851"
SELLER = SPEC.model_copy(
    update={"hiring_is_demand": True, "offer": "White-label web development for agencies"}
)


def _signal(url=POSTING, quote="Agency hiring a WordPress developer", source=DateSource.JOB_POSTING):
    return VerifiedSignal("hiring", "claim", url, quote, TrustedDate(date(2026, 11, 17), source))


def _assess(signals, spec, intents, kinds=None):
    return asyncio.run(
        assess_signals(
            signals, spec, "Agency", "agency.com", kinds or {POSTING: "job_ad"}, IntentLLM(intents)
        )
    )


def test_for_a_capacity_seller_a_posting_for_the_skill_is_a_buying_sign():
    hire = _signal()
    result = _assess([hire], SELLER, ["in_house_build"])
    assert result.buying == [hire]
    assert result.against == []
    assert hire.intent == HIRING_FOR_OFFER
    assert hire.intent_reason.startswith("hiring for work the client supplies")
    assert can_qualify(hire)
    assert INTENT_LABELS[HIRING_FOR_OFFER] == "Hiring for this skill (not a partner request)"


def test_without_the_setting_the_same_hire_still_counts_against():
    hire = _signal()
    result = _assess([hire], SPEC, ["in_house_build"])
    assert result.against == [hire]
    assert hire.intent == "in_house_build"


def test_only_a_job_posting_is_relabelled_never_a_news_item_or_an_unrelated_post():
    news_url = "https://press.example/agency-builds-in-house"
    news = _signal(url=news_url, quote="We build everything in-house")
    unrelated = _signal(quote="Agency hiring a receptionist")
    result = _assess(
        [news, unrelated], SELLER, ["in_house_build", "unrelated"], {POSTING: "job_ad", news_url: "news"}
    )
    assert result.against == [news, unrelated]
    assert news.intent == "in_house_build"  # a statement, not a vacancy: still against
    assert unrelated.intent == "unrelated"


def test_a_hire_makes_a_lead_good_never_strong():
    hire, other_hire = _signal(), _signal(quote="Agency hiring a freelance developer")
    hire.intent = other_hire.intent = HIRING_FOR_OFFER
    assert combine([hire]).tier == TIER_GOOD
    freelance = _signal(quote="freelance developer wanted")
    freelance.intent = "outsourcing_signal"
    assert combine([hire, freelance]).tier == TIER_GOOD  # one need seen twice, not two kinds of demand
    asks = _signal(quote="looking for a white-label partner")
    asks.intent = "asks_for_supplier"
    assert combine([hire, asks]).tier == TIER_STRONG
    assert "Hiring for this skill" in combine([hire]).summary


class JudgeLLM:
    extract_model = judge_model = "m"

    def __init__(self):
        self.instructions = self.data = ""

    async def parse(self, model, instructions, data, schema, **kw):
        assert schema is ScoreOut
        self.instructions, self.data = instructions, data
        return ScoreOut(
            fit_score=30,
            intent_score=25,
            timing_score=12,
            why_this_lead="w",
            outreach_angle="a",
            risks="r",
            cited_evidence_ids=[],
        )


@pytest.mark.parametrize(("spec", "told"), [(SELLER, True), (SPEC, False)])
def test_the_judge_is_told_the_rule_only_for_capacity_sellers(spec, told):
    from app.services.extraction_service import Extraction

    llm = JudgeLLM()
    asyncio.run(score(Extraction("Agency", "d", False), spec, llm, date(2026, 11, 18)))
    assert (CAPACITY_SELLER_RULE in llm.instructions) is told
    assert '"client_offer"' in llm.data


@pytest.mark.parametrize(
    ("ask", "expected"),
    [
        ("agencies needing white-label web, AI or software development", True),
        ("agencies that outsource development", True),
        ("SaaS firms wanting staff augmentation", True),
        ("UK firms needing a new website", False),
        ("3PLs hiring warehouse operatives", False),
    ],
)
def test_capacity_offers_are_detected_from_the_plan(ask, expected):
    assert sells_capacity(fallback_plan(ask, 5, "WW", "acme.com", "Acme", "test")) is expected


def test_the_setting_is_stored_with_the_plan_and_reaches_the_spec():
    from types import SimpleNamespace

    plan = fallback_plan("agencies needing white-label development", 5, "WW", "acme.com", "Acme", "test")
    plan.hiring_is_demand = True
    job = SimpleNamespace(
        ask=plan.ask,
        requested_count=5,
        target_countries=["WW"],
        client_name="Acme",
        client_website="acme.com",
        icp=plan_to_json(plan),
        freshness_days=60,
        personalized_email=False,
        cost_cap_inr=50.0,
    )
    again = plan_from_job(job)
    assert again.hiring_is_demand
    spec = again.to_spec("acme-com")
    assert spec.hiring_is_demand
    assert spec.offer == plan.offer
