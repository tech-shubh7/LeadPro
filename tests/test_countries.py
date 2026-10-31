from datetime import date, datetime

from app.countries import RegistryRecord, get_pack
from app.models.enums import AliveProof


def _record(entity_type="ltd", status="active"):
    return RegistryRecord("companies_house", "01234567", "ACME LTD", entity_type, status,
                          date(2015, 3, 1), datetime(2026, 10, 30))


def test_uk_active_ltd_allowed():
    assert get_pack("GB").outreach_decision(_record()).allowed


def test_uk_no_registry_match_blocked_as_possible_sole_trader():
    d = get_pack("gb").outreach_decision(None)
    assert not d.allowed and "sole trader" in d.reason


def test_uk_limited_partnership_and_dissolved_blocked():
    pack = get_pack("GB")
    assert not pack.outreach_decision(_record(entity_type="limited-partnership")).allowed
    assert not pack.outreach_decision(_record(status="dissolved")).allowed


def test_uk_alive_rules():
    pack = get_pack("GB")
    assert pack.is_alive({AliveProof.REGISTRY_ACTIVE, AliveProof.WEBSITE_LIVE})
    assert pack.is_alive({AliveProof.WEBSITE_LIVE, AliveProof.RECENT_DATED_SIGNAL})  # brand != registered name
    assert not pack.is_alive({AliveProof.WEBSITE_LIVE})
    assert not pack.is_alive({AliveProof.REGISTRY_ACTIVE, AliveProof.RECENT_DATED_SIGNAL})


def test_us_alive_on_web_evidence():
    pack = get_pack("US")
    assert pack.is_alive({AliveProof.WEBSITE_LIVE, AliveProof.MAPS_OPERATIONAL})
    assert not pack.is_alive({AliveProof.WEBSITE_LIVE})


def test_unknown_country_is_research_only():
    for code in ("DE", "SA", "CA"):
        assert not get_pack(code).outreach_decision(None).allowed
