import asyncio

import httpx
import respx

from app.core.cost import CostTracker
from app.models.enums import EmailVerdict
from app.providers.email.generect import GenerectFinder, to_verdict
from app.providers.registry.companies_house import (
    CompaniesHouseClient, find_company_numbers, normalize_company_name, parse_officer_name,
)


def test_generect_verdicts_never_treat_catch_all_as_valid():
    assert to_verdict("valid", False) == EmailVerdict.VALID
    assert to_verdict("valid", True) == EmailVerdict.CATCH_ALL
    assert to_verdict("catch_all", None) == EmailVerdict.CATCH_ALL
    assert to_verdict("unknown", False) == EmailVerdict.UNKNOWN
    assert to_verdict(None, None) == EmailVerdict.UNKNOWN
    # Live API sends catch_all as a string
    assert to_verdict("valid", "false") == EmailVerdict.VALID
    assert to_verdict("valid", "unknown") == EmailVerdict.VALID
    assert to_verdict("valid", "true") == EmailVerdict.CATCH_ALL
    assert to_verdict("unknown", "unknown") == EmailVerdict.UNKNOWN


@respx.mock
def test_generect_find_bills_only_when_found():
    route = respx.post("https://api.generect.com/api/v1/email/find/")
    route.side_effect = [
        httpx.Response(200, json={"data": {"valid_email": "Jane.Roe@acme.co.uk", "result": "valid", "catch_all": False}}),
        httpx.Response(200, json={"data": {"valid_email": None, "result": "unknown", "catch_all": "unknown"},
                                  "meta": {"amount_charged": 0.0}}),
    ]
    cost = CostTracker(cap_inr=50)
    finder = GenerectFinder(cost, client=httpx.AsyncClient(base_url="https://api.generect.com"))
    hit = asyncio.run(finder.find("Jane", "Roe", "acme.co.uk"))
    miss = asyncio.run(finder.find("Bob", "Smith", "acme.co.uk"))
    assert hit.email == "jane.roe@acme.co.uk" and hit.verdict == EmailVerdict.VALID
    assert miss.email is None and miss.verdict == EmailVerdict.UNKNOWN
    assert len(cost.lines) == 1  # the miss cost nothing


def test_company_number_and_officer_parsing():
    assert find_company_numbers("Acme Ltd. Registered in England No. 1234567. VAT 123") == ["01234567"]
    assert find_company_numbers("Company number: SC123456") == ["SC123456"]
    assert find_company_numbers("Registered in England and Wales No. 01234567") == ["01234567"]
    assert find_company_numbers("Company Registration Number 09876543") == ["09876543"]
    assert find_company_numbers("Call us on 0113 496 0000") == []
    assert find_company_numbers("Our company registration number is [11800189, Clockwise House]") == ["11800189"]
    assert parse_officer_name("ROE, Jane Mary") == ("Jane", "Roe")
    assert normalize_company_name("The Acme & Sons Limited") == normalize_company_name("ACME AND SONS LTD")


@respx.mock
def test_name_match_fails_closed_when_ambiguous():
    respx.get("https://api.company-information.service.gov.uk/search/companies").respond(200, json={"items": [
        {"title": "ACME LTD", "company_number": "01111111", "company_status": "active"},
        {"title": "Acme Limited", "company_number": "02222222", "company_status": "active"},
    ]})
    ch = CompaniesHouseClient(client=httpx.AsyncClient(base_url="https://api.company-information.service.gov.uk"))
    assert asyncio.run(ch.match_by_name("Acme")) is None


@respx.mock
def test_name_match_ignores_dissolved_namesake():
    respx.get("https://api.company-information.service.gov.uk/search/companies").respond(200, json={"items": [
        {"title": "ALP LIMITED", "company_number": "01701876", "company_status": "dissolved"},
    ]})
    ch = CompaniesHouseClient(client=httpx.AsyncClient(base_url="https://api.company-information.service.gov.uk"))
    assert asyncio.run(ch.match_by_name("ALP")) is None


@respx.mock
def test_google_news_redirect_resolved_to_publisher():
    from app.providers.search.serper import SerperClient
    goto = "https://www.google.com/goto?url=ABC123"
    respx.post("https://google.serper.dev/news").respond(200, json={"news": [
        {"title": "Brand raises £2m", "link": goto, "snippet": "Brand raised £2m.", "date": "2 days ago"}], "credits": 1})
    respx.get(goto).respond(302, headers={"location": "https://www.retail-gazette.co.uk/article/brand-raises"})
    async def main():
        serper = SerperClient(CostTracker(cap_inr=5), client=httpx.AsyncClient())
        [hit] = await serper.query("q", kind="news")
        return hit.link, await serper.resolve_redirect(hit.link)
    raw, resolved = asyncio.run(main())
    assert raw == goto  # not resolved for every hit (that caused Google to refuse bursts)
    assert resolved == "https://www.retail-gazette.co.uk/article/brand-raises"


def test_news_results_never_become_candidates_directly():
    from app.pipeline.discover import discover
    from app.pipeline.jobspec import JobSpec
    from app.providers.search.serper import SearchHit

    class Serper:
        async def query(self, q, kind="search", gl="uk", num=10, tbs=None):
            return [SearchHit("x", "https://brandnews.example.co.uk/story", "About Acme", None, "news", q)]

    class LLM:
        async def parse(self, model, instructions, data, schema, **k):
            from app.pipeline.discover import HitTriage, QueryPlanOut, TriageOut
            if schema is QueryPlanOut:
                return QueryPlanOut(queries=[])
            return TriageOut(items=[HitTriage(hit_index=0, company_name="Acme", entity_type="publisher",
                                              link_is_company_own_site=False, icp_fit=9)])

    spec = JobSpec.model_validate({"client": {"key": "k", "name": "n"}, "ask": "a", "count": 1, "icp": {},
        "playbook": {"signals": [{"type": "t", "description": "d", "queries": ["q"], "kind": "news"}], "buyer_titles": ["CEO"]}})
    assert asyncio.run(discover(spec, Serper(), LLM(), set(), 5)) == []
