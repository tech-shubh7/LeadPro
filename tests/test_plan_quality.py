"""Plan quality checks and the repair pass. The model is faked: these pin the rules, not the model."""

import asyncio

import pytest

from app.schemas.client_profile_schema import ClientProfile
from app.services.intake_service import Plan, QueryRepair, QueryRepairOut, repair_plan
from app.services.plan_quality_service import check_plan, required_phrases

NARROW = '"head of operations" "ecommerce" "United Kingdom" "hiring"'
GOOD = '"contract developer" careers site:co.uk'


def _plan(queries, **kw) -> Plan:
    signal = {"type": "hiring", "description": "d", "strength": "high", "kind": "search", "queries": queries}
    defaults = {
        "ask": "a",
        "count": 5,
        "country": "GB",
        "client_name": "K",
        "client_website": "k.in",
        "client_summary": "s",
        "offer": "o",
        "icp": {},
        "signals": [signal],
        "buyer_titles": ["founder"],
        "where_to_look": "why",
        "profile": ClientProfile(country="IN", country_evidence="t"),
    }
    return Plan(**(defaults | kw))


@pytest.mark.parametrize(
    ("query", "n"),
    [
        ('"a" "b" "c" -"x" -"y"', 3),
        ('"a" OR "b" OR "c" "d"', 2),
        ('site:co.uk "a"', 1),
        (NARROW, 4),
    ],
)
def test_required_phrases_counts_what_a_result_must_contain(query, n):
    assert required_phrases(query) == n


def test_each_flaw_is_reported():
    plan = _plan([NARROW, "site:*.co.uk x", "linkedin agency", '"invitation to tender" web', GOOD])
    assert {i.check for i in check_plan(plan)} == {
        "too_narrow",
        "site_wildcard",
        "blocked_platform",
        "ineligible_tenders",
    }


class RepairingLLM:
    extract_model = judge_model = "m"

    def __init__(self, fixes=None, fail=False):
        self.fixes, self.fail = fixes or {}, fail

    async def parse(self, model, instructions, data, schema, **kw):
        if self.fail:
            raise RuntimeError("model down")
        return QueryRepairOut(repairs=[QueryRepair(original=o, replacement=r) for o, r in self.fixes.items()])


def test_the_model_repairs_a_flawed_query():
    plan = asyncio.run(
        repair_plan(_plan([NARROW, GOOD]), RepairingLLM({NARROW: '"head of operations" ecommerce UK'}))
    )
    assert plan.signals[0]["queries"] == ['"head of operations" ecommerce UK', GOOD]
    assert check_plan(plan) == []


def test_a_query_the_model_cannot_fix_is_dropped():
    plan = asyncio.run(repair_plan(_plan([NARROW, GOOD]), RepairingLLM({NARROW: NARROW})))
    assert plan.signals[0]["queries"] == [GOOD]


def test_a_model_failure_still_leaves_no_flawed_query():
    plan = asyncio.run(repair_plan(_plan([NARROW, GOOD]), RepairingLLM(fail=True)))
    assert plan.signals[0]["queries"] == [GOOD]


def test_a_signal_with_only_flawed_queries_is_dropped():
    plan = asyncio.run(repair_plan(_plan([NARROW]), None))
    assert plan.signals == []


def test_a_plan_without_a_demand_signal_is_given_one():
    from app.services.intake_service import DraftedSignal, StrongSignalsOut

    class LLM(RepairingLLM):
        async def parse(self, model, instructions, data, schema, **kw):
            if schema is StrongSignalsOut:
                return StrongSignalsOut(
                    signals=[
                        DraftedSignal(
                            type="launching_new_product",
                            description="announced a launch",
                            strength="high",
                            queries=['"launches" new range India'],
                        )
                    ]
                )
            return await super().parse(model, instructions, data, schema, **kw)

    weak = _plan([GOOD])
    weak.signals[0]["strength"] = "medium"
    plan = asyncio.run(repair_plan(weak, LLM()))
    assert [s["strength"] for s in plan.signals] == ["medium", "high"]
    assert check_plan(plan) == []
