"""Browse sources (TinyFish agent) and source eligibility. HTTP mocked: nothing is spent."""

import asyncio

import httpx
import pytest
import respx

from app.providers.agents.tinyfish_agent_client import BASE_URL, TinyFishAgentClient
from app.schemas.client_profile_schema import ClientProfile
from app.schemas.job_spec_schema import SourceSpec
from app.services import source_service
from app.services.eligibility_service import apply_eligibility
from app.services.source_service import browse_url_problem
from app.utils.cost_tracker import CostTracker

OFFSHORE = ClientProfile(country="IN", country_evidence="test")
COHORT = {
    "type": "agent_browse",
    "url": "https://www.example-accelerator.co.uk/cohort-2026",
    "goal": "List the startups in this cohort",
    "why": "new startups need a site",
}
AWARDS = {"type": "contracts_awards", "keywords": ["website"], "why": "winners need capacity"}


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/company/x",
        "https://uk.indeed.com/jobs",
        "https://www.peopleperhour.com/freelance-jobs",
        "ftp://x.com",
        None,
    ],
)
def test_login_walls_aggregators_and_bad_urls_are_never_browsed(url):
    assert browse_url_problem(url)


def test_a_public_list_page_may_be_browsed():
    assert browse_url_problem(COHORT["url"]) is None


def test_award_winners_stay_for_an_offshore_client_but_tender_portals_do_not():
    portal = {**COHORT, "url": "https://www.find-tender.service.gov.uk/Search", "goal": "list open tenders"}
    result = apply_eligibility([], OFFSHORE, "GB", [COHORT, AWARDS, portal])
    assert [s["type"] for s in result.sources] == ["agent_browse", "contracts_awards"]
    assert any("tender" in e.why.lower() for e in result.exclusions)


@respx.mock
def test_the_agent_reserves_its_step_cap_and_records_only_steps_used():
    respx.post(f"{BASE_URL}/v1/automation/run").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "COMPLETED",
                "num_of_steps": 5,
                "result": {
                    "companies": [
                        {
                            "company_name": "Brightwave",
                            "company_website": "brightwave.co.uk",
                            "evidence_url": "https://x.co.uk/cohort",
                            "evidence_text": "Brightwave builds robots",
                        },
                        {
                            "company_name": "",
                            "evidence_url": "https://x",
                            "evidence_text": "no name: dropped",
                        },
                    ]
                },
            },
        )
    )
    cost = CostTracker(cap_inr=100)
    agent = TinyFishAgentClient(cost, client=httpx.AsyncClient(base_url=BASE_URL))
    rows = asyncio.run(agent.list_companies(COHORT["url"], "list startups", max_steps=12, limit=10))
    assert [r.company_name for r in rows] == ["Brightwave"]
    assert cost.lines[-1].units == 5
    assert cost.reserved_inr == 0


@respx.mock
def test_a_failed_agent_run_gives_no_companies():
    respx.post(f"{BASE_URL}/v1/automation/run").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "FAILED",
                "num_of_steps": 3,
                "result": None,
                "error": {"message": "bot challenge", "category": "AGENT_FAILURE"},
            },
        )
    )
    agent = TinyFishAgentClient(CostTracker(cap_inr=100), client=httpx.AsyncClient(base_url=BASE_URL))
    assert asyncio.run(agent.list_companies(COHORT["url"], "g", 12, 10)) == []


def test_browsed_companies_become_candidates_to_verify(monkeypatch):
    from app.providers.agents.tinyfish_agent_client import BrowsedCompany

    async def allowed(url):
        return True

    monkeypatch.setattr("app.providers.web.page_fetcher.robots_allowed", allowed)

    class Agent:
        async def list_companies(self, url, goal, max_steps, limit):
            return [
                BrowsedCompany(
                    "Brightwave",
                    "https://brightwave.co.uk",
                    "https://x.co.uk/cohort",
                    "Brightwave builds warehouse robots",
                    "",
                )
            ]

    [cand] = asyncio.run(source_service._browse_candidates(SourceSpec(**COHORT), Agent()))
    assert cand.domain == "brightwave.co.uk"
    assert cand.seed_hits[0].link == "https://x.co.uk/cohort"  # re-fetched and quote-checked later


def test_robots_disallow_skips_the_page_without_spending(monkeypatch):
    async def disallowed(url):
        return False

    monkeypatch.setattr("app.providers.web.page_fetcher.robots_allowed", disallowed)

    class Agent:
        async def list_companies(self, *a):
            raise AssertionError("must not run")

    assert asyncio.run(source_service._browse_candidates(SourceSpec(**COHORT), Agent())) == []


# --- when the buyers are themselves service businesses ---------------------------------------------

from app.schemas.job_spec_schema import JobSpec  # noqa: E402
from app.services.discovery_service import HitTriage, icp_is_service_businesses, is_target  # noqa: E402


def _spec(ask, industries=()):
    return JobSpec.model_validate(
        {
            "client": {"key": "k", "name": "K"},
            "ask": ask,
            "count": 5,
            "icp": {"industries": list(industries)},
            "playbook": {"signals": [], "buyer_titles": ["md"]},
        }
    )


def _triage(entity, fit):
    return HitTriage(
        hit_index=0,
        company_name="Pixel Agency",
        entity_type=entity,
        link_is_company_own_site=True,
        icp_fit=fit,
    )


def test_an_agency_is_a_target_when_agencies_are_who_we_want():
    """Job 24: asked for UK agencies, and every agency was dropped as a 'service provider'."""
    agencies = _spec("UK web design and marketing agencies needing a white-label partner")
    assert icp_is_service_businesses(agencies)
    assert is_target(_triage("service_provider", 8), agencies)
    assert not is_target(_triage("service_provider", 4), agencies)  # a poor fit still is not


def test_a_service_provider_is_still_skipped_when_the_buyers_are_not_service_businesses():
    retailers = _spec("UK e-commerce brands outgrowing their own fulfilment", ["retail", "D2C brands"])
    assert not icp_is_service_businesses(retailers)
    assert not is_target(_triage("service_provider", 9), retailers)
    assert is_target(_triage("target_company", 6), retailers)
