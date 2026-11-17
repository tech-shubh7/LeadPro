"""Normalization of domains, emails and phone numbers."""

import re
from dataclasses import dataclass

import phonenumbers
import tldextract
from phonenumbers import PhoneNumberType

from app.models.enums import PhoneSource, PhoneType

# Bundled public-suffix snapshot only: no network call at import time.
_extract = tldextract.TLDExtract(suffix_list_urls=())

_WA_LINK = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=)\+?(\d{8,15})", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_FILE_SUFFIX = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|css|js)$", re.I)
ROLE_LOCAL_PARTS = frozenset(
    {
        "info",
        "sales",
        "contact",
        "hello",
        "admin",
        "support",
        "enquiries",
        "enquiry",
        "office",
        "hr",
        "careers",
        "jobs",
        "team",
        "mail",
        "marketing",
        "accounts",
        "help",
        "noreply",
        "no-reply",
        "privacy",
        "gdpr",
        "dpo",
        "press",
        "media",
        "billing",
        "invoices",
        "orders",
        "service",
        "customerservice",
        "reception",
        "general",
        "webmaster",
        "recruitment",
    }
)


def normalize_domain(url_or_domain: str) -> str | None:
    """'https://www.Shop.Example.co.uk/about' -> 'example.co.uk' (registered domain)."""
    parts = _extract(url_or_domain.strip().lower())
    if not parts.domain or not parts.suffix:
        return None
    return f"{parts.domain}.{parts.suffix}"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def extract_emails(text: str, domain: str | None = None) -> list[str]:
    found = {normalize_email(m) for m in _EMAIL.findall(text) if not _FILE_SUFFIX.search(m)}
    if domain:
        found = {e for e in found if normalize_domain(e.split("@", 1)[1]) == domain}
    return sorted(found)


def is_role_email(email: str) -> bool:
    return email.split("@", 1)[0] in ROLE_LOCAL_PARTS


def email_matches_person(email: str, first_name: str, last_name: str) -> bool:
    """True if the local part clearly belongs to this person (not a role inbox)."""
    local = re.sub(r"[^a-z]", "", email.split("@", 1)[0].lower())
    first = re.sub(r"[^a-z]", "", first_name.lower())
    last = re.sub(r"[^a-z]", "", last_name.lower())
    if not first or is_role_email(email):
        return False
    candidates = {first + last, last + first, first[0] + last if last else "", first + last[:1], first}
    candidates.discard("")
    return local in candidates or (bool(last) and first in local and last in local)


@dataclass(frozen=True)
class PhoneHit:
    e164: str
    phone_type: PhoneType
    source: PhoneSource
    raw: str


def _phone_type(number: phonenumbers.PhoneNumber) -> PhoneType:
    kind = phonenumbers.number_type(number)
    if kind == PhoneNumberType.MOBILE:
        return PhoneType.MOBILE
    if kind in (PhoneNumberType.FIXED_LINE, PhoneNumberType.FIXED_LINE_OR_MOBILE):
        return PhoneType.LANDLINE
    return PhoneType.OTHER


def extract_phones(text: str, region: str) -> list[PhoneHit]:
    """Valid phone numbers from page text/HTML, including wa.me WhatsApp links.

    Uses libphonenumber's matcher instead of a bare digit regex, which catches
    order numbers and GST/ID strings and misses spaced formats.
    """
    hits: dict[str, PhoneHit] = {}
    for m in _WA_LINK.finditer(text):
        try:
            num = phonenumbers.parse("+" + m.group(1), None)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(num):
            e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
            hits[e164] = PhoneHit(e164, _phone_type(num), PhoneSource.WHATSAPP_LINK, m.group(0))
    for match in phonenumbers.PhoneNumberMatcher(text, region):
        num = match.number
        if not phonenumbers.is_valid_number(num):
            continue
        e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
        hits.setdefault(e164, PhoneHit(e164, _phone_type(num), PhoneSource.COMPANY_SITE, match.raw_string))
    return list(hits.values())
