"""Hunter email provider and the finder waterfall. HTTP is mocked: no credits are spent."""

import asyncio

import httpx
import pytest
import respx

from app.config.settings import get_settings
from app.models.enums import EmailVerdict
from app.providers.email import hunter_provider
from app.providers.email.email_finder_factory import create_email_finders
from app.providers.email.hunter_provider import BASE_URL, HunterProvider, to_verdict
from app.utils.cost_tracker import CostTracker


def _hunter() -> HunterProvider:
    return HunterProvider(CostTracker(cap_inr=100), client=httpx.AsyncClient(base_url=BASE_URL))


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        ("valid", EmailVerdict.VALID),
        ("accept_all", EmailVerdict.CATCH_ALL),
        ("invalid", EmailVerdict.INVALID),
        ("disposable", EmailVerdict.INVALID),
        ("webmail", EmailVerdict.UNKNOWN),
        ("unknown", EmailVerdict.UNKNOWN),
        (None, EmailVerdict.UNKNOWN),
    ],
)
def test_only_a_valid_status_is_a_deliverable_address(status, verdict):
    assert to_verdict(status) == verdict


@respx.mock
def test_a_found_and_verified_email_is_valid_and_charged():
    respx.get(f"{BASE_URL}/email-finder").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"email": "Dana.Lee@Acme.co.uk", "score": 97, "verification": {"status": "valid"}}},
        )
    )
    hunter = _hunter()
    result = _run(hunter.find("Dana", "Lee", "acme.co.uk"))
    assert result.email == "dana.lee@acme.co.uk"
    assert result.verdict == EmailVerdict.VALID
    assert hunter.cost.lines[-1].provider == "hunter"
    assert hunter.cost.reserved_inr == 0


@respx.mock
def test_nothing_found_is_not_charged():
    respx.get(f"{BASE_URL}/email-finder").mock(
        return_value=httpx.Response(200, json={"data": {"email": None}})
    )
    hunter = _hunter()
    result = _run(hunter.find("Dana", "Lee", "acme.co.uk"))
    assert result.email is None
    assert result.verdict == EmailVerdict.UNKNOWN
    assert not hunter.cost.lines


@respx.mock
def test_an_accept_all_domain_is_catch_all_never_valid():
    respx.get(f"{BASE_URL}/email-finder").mock(
        return_value=httpx.Response(
            200, json={"data": {"email": "dana@acme.co.uk", "verification": {"status": "accept_all"}}}
        )
    )
    assert _run(_hunter().find("Dana", "Lee", "acme.co.uk")).verdict == EmailVerdict.CATCH_ALL


@respx.mock
def test_an_exhausted_plan_stops_hunter_for_the_rest_of_the_run():
    route = respx.get(f"{BASE_URL}/email-finder").mock(return_value=httpx.Response(429, json={}))
    hunter = _hunter()
    assert _run(hunter.find("Dana", "Lee", "acme.co.uk")).raw_status == "exhausted"
    calls = route.call_count
    _run(hunter.find("Sam", "Ray", "acme.co.uk"))
    assert hunter.exhausted
    assert route.call_count == calls


@respx.mock
def test_the_rate_limit_is_waited_out_once(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(hunter_provider.asyncio, "sleep", instant)
    respx.get(f"{BASE_URL}/email-finder").mock(
        side_effect=[
            httpx.Response(403, json={}),
            httpx.Response(
                200, json={"data": {"email": "dana@acme.co.uk", "verification": {"status": "valid"}}}
            ),
        ]
    )
    assert _run(_hunter().find("Dana", "Lee", "acme.co.uk")).verdict == EmailVerdict.VALID


@respx.mock
def test_verification_still_running_is_unknown_not_valid():
    respx.get(f"{BASE_URL}/email-verifier").mock(return_value=httpx.Response(202, json={}))
    [result] = _run(_hunter().validate(["dana@acme.co.uk"]))
    assert result.verdict == EmailVerdict.UNKNOWN
    assert result.raw_status == "pending"


def test_the_waterfall_follows_settings_and_skips_finders_without_a_key(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "email_finder_order", ["hunter", "generect"])
    monkeypatch.setattr(settings, "hunter_api_key", "k")
    monkeypatch.setattr(settings, "generect_api_key", "")
    assert [f.name for f in create_email_finders(CostTracker(cap_inr=10))] == ["hunter"]
    monkeypatch.setattr(settings, "email_finder_order", ["nope"])
    with pytest.raises(ValueError, match="unknown email finder"):
        create_email_finders(CostTracker(cap_inr=10))
