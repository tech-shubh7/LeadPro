"""Eligibility: a client is never sent after channels it cannot sell through. Deterministic, no model."""

import pytest

from app.schemas.client_profile_schema import ClientFactsOut, ClientProfile
from app.services.eligibility_service import (
    PROCUREMENT,
    apply_eligibility,
    can_bid_public_tenders,
    verify_client_facts,
)
from app.services.intake_service import fallback_plan

BRIGHTPATH_SITE = (
    "Brightpath Web is a web and mobile app development company based in Pune, India. "
    "Call us on +91 79 4006 1111 or +91 98250 12345. We build for agencies worldwide."
)
UK_COUNCIL_SUPPLIER_SITE = (
    "Acme Digital, 12 Park Row, Leeds LS1 5HD. Trusted by Leeds City Council and the Cabinet Office "
    "for accessible public-sector websites. Call 0113 496 0000."
)
TENDER_SIGNAL = {
    "type": "public_tender_published",
    "description": "A council publishes a tender for a new website",
    "strength": "very_high",
    "kind": "search",
    "queries": ['"invitation to tender" website redesign site:co.uk', '"contract notice" web development'],
}
MIXED_SIGNAL = {
    "type": "asking_for_proposals",
    "description": "A company asks for proposals for a website",
    "strength": "very_high",
    "kind": "search",
    "queries": ['"request for proposal" website "submission deadline"', '"tender" new website council'],
}
HIRING_SIGNAL = {
    "type": "hiring_contract_developer",
    "description": "Hiring contract developers",
    "strength": "high",
    "kind": "search",
    "queries": ['"contract developer" careers site:co.uk'],
}


def _brightpathweb() -> ClientProfile:
    facts = ClientFactsOut(based_in_country="IN", based_in_quote="based in Pune, India")
    return verify_client_facts(facts, BRIGHTPATH_SITE, "brightpathweb-example.com")


# --- the exact mistake that started this ---------------------------------------------------------


def test_an_offshore_agency_never_gets_uk_public_tenders():
    result = apply_eligibility([TENDER_SIGNAL, MIXED_SIGNAL, HIRING_SIGNAL], _brightpathweb(), "GB")
    queries = [q for s in result.signals for q in s["queries"]]
    assert not any(PROCUREMENT.search(q) for q in queries)
    assert [s["type"] for s in result.signals] == ["asking_for_proposals", "hiring_contract_developer"]
    assert all("based in IN" in e.why for e in result.exclusions)  # the operator sees why


def test_the_fallback_template_respects_eligibility_too():
    """The no-model template used to put an 'invitation to tender' search in every plan."""
    plan = fallback_plan(
        "UK companies needing a website", 5, "GB", "brightpathweb-example.in", "Brightpath Web", "test", BRIGHTPATH_SITE
    )
    queries = [q for s in plan.signals for q in s["queries"]]
    assert queries
    assert not any(PROCUREMENT.search(q) for q in queries)
    assert plan.exclusions


# --- who may bid ---------------------------------------------------------------------------------


def test_a_local_supplier_with_public_sector_work_keeps_tenders():
    facts = ClientFactsOut(
        based_in_country="GB",
        based_in_quote="12 Park Row, Leeds LS1 5HD",
        public_sector_quote="Trusted by Leeds City Council and the Cabinet Office",
    )
    profile = verify_client_facts(facts, UK_COUNCIL_SUPPLIER_SITE, "acme-digital.co.uk")
    assert can_bid_public_tenders(profile, "GB") == (True, "")
    assert apply_eligibility([TENDER_SIGNAL], profile, "GB").signals == [TENDER_SIGNAL]


def test_a_local_supplier_without_public_sector_work_does_not_get_tenders():
    profile = ClientProfile(country="GB", country_evidence="test")
    allowed, why = can_bid_public_tenders(profile, "GB")
    assert not allowed
    assert "no public-sector work" in why


def test_unknown_location_and_no_public_record_is_treated_as_not_eligible():
    assert not can_bid_public_tenders(ClientProfile(), "GB")[0]


# --- facts must be on the site -------------------------------------------------------------------


def test_an_invented_location_quote_is_not_trusted_and_hints_take_over():
    facts = ClientFactsOut(based_in_country="GB", based_in_quote="our London headquarters")  # not on the site
    profile = verify_client_facts(facts, BRIGHTPATH_SITE, "brightpathweb-example.com")
    assert profile.country == "IN"  # from the +91 numbers, not the invented quote
    assert "phone" in profile.country_evidence


def test_an_invented_public_sector_claim_unlocks_nothing():
    facts = ClientFactsOut(
        based_in_country="GB",
        based_in_quote="12 Park Row, Leeds LS1 5HD",
        public_sector_quote="Supplier to NHS England since 2015",
    )  # not on the site
    profile = verify_client_facts(
        facts,
        UK_COUNCIL_SUPPLIER_SITE.replace("Leeds City Council and the Cabinet Office for", "clients for"),
        "acme-digital.co.uk",
    )
    assert not profile.has_public_sector_record
    assert not can_bid_public_tenders(profile, "GB")[0]


@pytest.mark.parametrize(
    ("website", "country"),
    [
        ("brightpathweb-example.in", "IN"),
        ("acme.co.uk", "GB"),
        ("acme.com.au", "AU"),
        ("acme.de", "DE"),
    ],
)
def test_a_country_code_domain_locates_the_client(website, country):
    assert verify_client_facts(ClientFactsOut(), "", website).country == country


def test_a_generic_domain_with_mixed_phone_countries_stays_unknown():
    text = "Offices: +91 79 4006 1111 and +44 20 7946 0000"
    assert verify_client_facts(ClientFactsOut(), text, "acme.com").country == ""


# --- what counts as procurement --------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        '"invitation to tender" website',
        "site:find-tender.service.gov.uk web",
        "contracts finder digital",
        "council website redesign tender",
        "G-Cloud web development",
        '"framework agreement" software',
        "NHS trust app procurement",
    ],
)
def test_procurement_searches_are_recognised(query):
    assert PROCUREMENT.search(query)


@pytest.mark.parametrize(
    "query",
    [
        '"request for proposal" website "submission deadline"',
        '"looking for an agency" Shopify',
        '"contract developer" careers',
        "raises seed funding to build platform",
        "tenderloin restaurant website",
    ],
)
def test_private_demand_is_not_mistaken_for_procurement(query):
    assert not PROCUREMENT.search(query)


def test_enforcement_is_idempotent():
    plan = fallback_plan("UK firms", 5, "GB", "brightpathweb-example.in", "Brightpath Web", "test", BRIGHTPATH_SITE)
    before = (list(plan.signals), list(plan.exclusions))
    plan.enforce_eligibility()
    assert (plan.signals, plan.exclusions) == before


# --- the client is read once ---------------------------------------------------------------------


def test_a_client_website_is_read_once_and_then_reused(clean_db):
    """Seven Brightpath Web runs used to re-read and re-pay for the same website every time."""
    import asyncio

    from app.services.intake_service import ClientBriefOut, PlanOut, draft_plan
    from scripts.eval_planner import FixtureFetcher

    class LLM:
        extract_model = judge_model = "m"

        def __init__(self):
            self.calls = []

        async def parse(self, model, instructions, data, schema, **kw):
            self.calls.append(schema.__name__)
            if schema is ClientBriefOut:
                return ClientBriefOut(
                    client_facts=ClientFactsOut(
                        based_in_country="IN", based_in_quote="based in Pune, India"
                    ),
                    client_summary="Brightpath Web builds websites",
                    offer="web development",
                )
            assert "Brightpath Web builds websites" in data  # the planner reads the short brief...
            assert "+91 79 4006 1111" not in data  # ...not the website itself
            return PlanOut.model_validate(
                {
                    "offer": "web development",
                    "icp": {
                        "industries": ["retail"],
                        "geographies": ["UK"],
                        "size_band": "10-200",
                        "business_model": "B2B",
                        "why_they_buy": ["x"],
                    },
                    "signals": [
                        {
                            "type": "hiring_contract_developer",
                            "description": "d",
                            "strength": "high",
                            "queries": ['"contract developer" careers'],
                        }
                    ],
                    "buyer_titles": ["founder"],
                    "where_to_look": "job posts",
                }
            )

    llm = LLM()
    fetcher = FixtureFetcher("brightpathweb-example.com", BRIGHTPATH_SITE)
    first = asyncio.run(
        draft_plan("brightpathweb-example.com", "UK firms", 5, "GB", llm, fetcher, client_key="brightpathweb-example-com")
    )
    second = asyncio.run(
        draft_plan("brightpathweb-example.com", "UK agencies", 5, "GB", llm, fetcher, client_key="brightpathweb-example-com")
    )
    assert llm.calls == ["ClientBriefOut", "PlanOut", "PlanOut"]  # the website was understood once
    assert first.profile.country == second.profile.country == "IN"


def test_no_website_plans_from_the_request_alone_and_safely():
    import asyncio

    from app.services.intake_service import learn_client

    brief, _ = asyncio.run(learn_client(None, "acme", None, None))
    assert brief.summary == ""
    assert not can_bid_public_tenders(brief.profile, "GB")[0]
