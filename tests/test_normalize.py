from app.models.enums import PhoneSource, PhoneType
from app.utils.normalize import (
    email_matches_person,
    extract_emails,
    extract_phones,
    is_role_email,
    normalize_domain,
)


def test_normalize_domain():
    assert normalize_domain("https://www.Shop.Example.co.uk/about") == "example.co.uk"
    assert normalize_domain("acme.com") == "acme.com"
    assert normalize_domain("not a domain") is None


def test_extract_emails_filters_to_company_domain():
    text = "Write to jane.roe@acme.co.uk or info@acme.co.uk, not bob@gmail.com"
    assert extract_emails(text, "acme.co.uk") == ["info@acme.co.uk", "jane.roe@acme.co.uk"]


def test_role_vs_personal_email():
    assert is_role_email("sales@acme.co.uk")
    assert email_matches_person("jane.roe@acme.co.uk", "Jane", "Roe")
    assert email_matches_person("jroe@acme.co.uk", "Jane", "Roe")
    assert not email_matches_person("info@acme.co.uk", "Jane", "Roe")
    assert not email_matches_person("bob@acme.co.uk", "Jane", "Roe")


def test_uk_phones_and_whatsapp():
    text = "Call 020 7946 0958 or mobile 07911 123456. Order no 70123456789012. wa.me/447400123456"
    hits = {h.e164: h for h in extract_phones(text, "GB")}
    assert hits["+442079460958"].phone_type == PhoneType.LANDLINE
    assert hits["+447911123456"].phone_type == PhoneType.MOBILE
    assert hits["+447400123456"].source == PhoneSource.WHATSAPP_LINK
    assert not any("7012345678" in e for e in hits)


def test_ofcom_fictional_numbers_rejected():
    assert extract_phones("Call 07700 900123", "GB") == []
