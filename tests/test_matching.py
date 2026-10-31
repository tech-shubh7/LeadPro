"""Registry matching by printed legal name, and brand-name -> domain resolution."""
import asyncio
from types import SimpleNamespace

from app.countries.base import REGISTRY_MATCH_LEGAL_NAME, REGISTRY_MATCH_NAME, REGISTRY_MATCH_NUMBER
from app.countries.uk import UKPack
from app.pipeline.discover import resolve_domain
from app.pipeline.runner import _registry_record
from app.providers.registry.companies_house import find_legal_names
from app.providers.search.serper import SearchHit


def test_legal_names_are_read_from_site_text_only_when_they_belong_to_the_domain():
    assert find_legal_names("© 2026 Doughlicious Limited. All rights reserved.", "doughlicious.co.uk") == ["Doughlicious Limited"]
    assert find_legal_names("All Rights Reserved Pip And Nut Ltd", "pipandnut.com") == ["Pip And Nut Ltd"]
    assert find_legal_names("Website by Motive Media Limited", "pipandnut.com") == []  # agency credit: not the company
    assert find_legal_names("P Louise Cosmetics Ltd, Manchester", "p-louise.com") == ["P Louise Cosmetics Ltd"]
    assert find_legal_names("a limited edition range", "acme.co.uk") == []


class _Registry:
    def __init__(self, by_number=None, by_name=None):
        self.by_number, self.by_name, self.asked = by_number or {}, by_name or {}, []

    async def profile(self, number):
        return self.by_number.get(number)

    async def match_by_name(self, name):
        self.asked.append(name)
        return self.by_name.get(name)

    async def match_by_officer(self, first, last, words):
        return None


def _match(registry, numbers=(), legal=(), brand="P.Louise"):
    deps = SimpleNamespace(registry=registry)
    bundle = SimpleNamespace(company_numbers=list(numbers), legal_names=list(legal), domain="p-louise.com")
    return asyncio.run(_registry_record(deps, UKPack(), bundle, brand))


def test_registry_match_order_number_then_printed_legal_name_then_brand():
    reg = _Registry(by_number={"01234567": "N"}, by_name={"P Louise Cosmetics Limited": "L", "P.Louise": "B"})
    assert _match(reg, numbers=["01234567"], legal=["P Louise Cosmetics Limited"]) == ("N", REGISTRY_MATCH_NUMBER)
    assert _match(reg, legal=["P Louise Cosmetics Limited"]) == ("L", REGISTRY_MATCH_LEGAL_NAME)
    assert _match(reg) == ("B", REGISTRY_MATCH_NAME)
    assert _match(_Registry()) == (None, None)


class _Serper:
    def __init__(self, hits):
        self.hits = hits

    async def query(self, q, **kw):
        return self.hits


def _hit(link, title, snippet=""):
    return SearchHit(title, link, snippet, None, "search", "q")


def test_domain_resolution_prefers_a_result_about_the_right_kind_of_business():
    hits = [_hit("https://www.trip.com/", "Trip.com | Flights, Hotels", "Book cheap flights"),
            _hit("https://drinktrip.com/", "TRIP | CBD drinks", "Calming CBD drinks and sparkling infusions")]
    assert asyncio.run(resolve_domain(_Serper(hits), "TRIP", "uk", "CBD drinks brand")) == "drinktrip.com"
    # nothing on topic: falls back to the old name rule rather than dropping the company
    assert asyncio.run(resolve_domain(_Serper(hits[:1]), "TRIP", "uk", "CBD drinks brand")) == "trip.com"


import httpx  # noqa: E402
import respx  # noqa: E402

from app.providers.registry.companies_house import CompaniesHouseClient  # noqa: E402

CH = "https://api.company-information.service.gov.uk"


def _officer_api(appointments):
    respx.get(f"{CH}/search/officers").respond(200, json={"items": [
        {"title": "Harry James RADFORD", "links": {"self": "/officers/abc/appointments"}},
        {"title": "Harriet RADFORD", "links": {"self": "/officers/zzz/appointments"}}]})
    respx.get(f"{CH}/officers/abc/appointments").respond(200, json={"items": appointments})
    respx.get(f"{CH}/company/01234567").respond(200, json={"company_number": "01234567", "company_name": "EDENMOOR LTD",
                                                           "type": "ltd", "company_status": "active"})


def _ch():
    return CompaniesHouseClient(client=httpx.AsyncClient(base_url=CH))


@respx.mock
def test_officer_match_needs_a_current_appointment_at_an_active_company_with_the_brand_name():
    _officer_api([
        {"appointed_to": {"company_name": "EDENMOOR LTD", "company_number": "01234567", "company_status": "active"}},
        {"appointed_to": {"company_name": "RADFORD HOLDINGS LTD", "company_number": "07777777", "company_status": "active"}},
        {"appointed_to": {"company_name": "EDENMOOR TRADING LTD", "company_number": "08888888", "company_status": "active"},
         "resigned_on": "2020-01-01"}])
    record = asyncio.run(_ch().match_by_officer("Harry", "Radford", {"edenmoor"}))
    assert record.registry_id == "01234567"


@respx.mock
def test_officer_match_refuses_when_ambiguous():
    _officer_api([
        {"appointed_to": {"company_name": "EDENMOOR LTD", "company_number": "01234567", "company_status": "active"}},
        {"appointed_to": {"company_name": "EDENMOOR FARMS LTD", "company_number": "02222222", "company_status": "active"}}])
    assert asyncio.run(_ch().match_by_officer("Harry", "Radford", {"edenmoor"})) is None


def test_a_failed_email_lookup_marks_that_person_and_keeps_the_lead():
    from app.models.enums import BuyerRole
    from app.pipeline import contacts
    from app.pipeline.contacts import DecisionMaker, attach_emails

    class Slow:
        name = "slow"

        async def find(self, first, last, domain):
            raise httpx.ReadTimeout("slow")

        async def validate(self, emails):
            return []

    async def mx(domain):
        return True

    people = [DecisionMaker("Jo", "Bloggs", "MD", BuyerRole.ECONOMIC_BUYER, "u", "q")]
    orig, contacts.has_mx = contacts.has_mx, mx
    try:
        asyncio.run(attach_emails(people, [], "acme.co.uk", [Slow()]))
    finally:
        contacts.has_mx = orig
    assert people[0].email is None and people[0].finder_status == "error: ReadTimeout"
