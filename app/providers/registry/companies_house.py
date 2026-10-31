"""Companies House public data API (free). Auth: HTTP Basic, API key as username, blank password.
Paths and fields from the official swagger specs: /search/companies (items: title, company_number,
company_type, company_status, date_of_creation), /company/{n} (company_name, type, company_status,
date_of_creation, sic_codes), /company/{n}/officers (items: name, officer_role, appointed_on, resigned_on).
"""
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from app.core.http import request_with_retry

from app.config import get_settings
from app.countries.base import RegistryRecord

BASE_URL = "https://api.company-information.service.gov.uk"
# Officer roles that can act as decision-makers (values from api-enumerations constants.yml).
DECISION_ROLES = frozenset({"director", "llp-designated-member", "llp-member", "managing-officer"})
COMPANY_NUMBER_RE = re.compile(
    r"(?:company|registration|registered)[^\n\d]{0,40}?(?:no\.?|number|#)\s*(?:is\s*)?[:.\[(]?\s*((?:SC|NI|OC|SO|NC|R0)?\d{6,8})\b", re.I
)
_SUFFIXES = re.compile(r"\b(limited|ltd|plc|llp|l\.l\.p|uk|the|group|holdings)\b\.?", re.I)


def normalize_company_name(name: str) -> str:
    name = _SUFFIXES.sub(" ", name.lower().replace("&", " and "))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", name).split())


def find_company_numbers(text: str) -> list[str]:
    """Company numbers printed in site footers/legal pages, e.g. 'Registered in England No. 01234567'."""
    seen: list[str] = []
    for m in COMPANY_NUMBER_RE.finditer(text):
        number = m.group(1).upper()
        number = number.zfill(8) if number.isdigit() else number
        if number not in seen:
            seen.append(number)
    return seen


LEGAL_NAME_RE = re.compile(
    r"((?:[A-Z][A-Za-z0-9&.'\u2019-]*\s+){1,6})(Limited|LIMITED|Ltd|LTD|LLP|PLC|plc)\b")


_BOILERPLATE = re.compile(r"^(?:(?:all|rights|reserved|copyright|registered|company|website|operated|by|is|a|trading|as)\s+)+", re.I)


def find_legal_names(text: str, domain: str) -> list[str]:
    """Registered names printed on a company's own pages ('(c) 2026 Doughlicious Limited') that belong to
    that site's domain. A name unrelated to the domain (a supplier, an agency credit) is ignored, since an
    exact match to it would attach the wrong company and its directors."""
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    out: list[str] = []
    for m in LEGAL_NAME_RE.finditer(text):
        name = f"{_BOILERPLATE.sub('', m.group(1)).strip()} {m.group(2)}"
        compact = normalize_company_name(name).replace(" ", "")
        related = len(label) >= 4 and (label in compact or (len(compact) >= 5 and compact in label))
        if related and name not in out:
            out.append(name)
    return out[:3]


@dataclass(frozen=True)
class Officer:
    first_name: str
    last_name: str
    role: str
    appointed_on: date | None


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def parse_officer_name(name: str) -> tuple[str, str]:
    """Companies House lists people as 'SURNAME, Forename Middle'."""
    if "," in name:
        last, rest = name.split(",", 1)
        first = rest.split()[0] if rest.split() else ""
        return first.strip().title(), last.strip().title()
    parts = name.split()
    return (parts[0].title(), parts[-1].title()) if len(parts) > 1 else (name.title(), "")


class CompaniesHouseClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        key = get_settings().companies_house_api_key
        self.client = client or httpx.AsyncClient(base_url=BASE_URL, auth=(key, ""), timeout=20)

    async def profile(self, number: str) -> RegistryRecord | None:
        resp = await request_with_retry(self.client, "GET", f"/company/{number}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        d = resp.json()
        return RegistryRecord(
            registry="companies_house",
            registry_id=d.get("company_number", number),
            legal_name=d.get("company_name", ""),
            entity_type=d.get("type", ""),
            status=d.get("company_status", ""),
            incorporated_on=_parse_date(d.get("date_of_creation")),
            checked_at=datetime.now(UTC),
            raw=d,
        )

    async def match_by_name(self, name: str) -> RegistryRecord | None:
        """Exact normalized-name match to exactly one ACTIVE company, else None.

        A dissolved company that merely shares a brand's name is usually unrelated, so a
        name-only match never causes a rejection."""
        resp = await request_with_retry(self.client, "GET", "/search/companies", params={"q": name, "items_per_page": 20})
        resp.raise_for_status()
        target = normalize_company_name(name)
        exact = [i for i in resp.json().get("items", []) if normalize_company_name(i.get("title", "")) == target]
        active = [i for i in exact if i.get("company_status") == "active"]
        if len(active) != 1:
            return None
        return await self.profile(active[0]["company_number"])

    async def match_by_officer(self, first: str, last: str, brand_words: set[str]) -> RegistryRecord | None:
        """The one active company where this person (already quote-verified as working at the target) is a
        current officer AND whose registered name shares a distinctive word with the brand or domain.
        Brand names often differ from registered names; the officer link is what ties them together."""
        resp = await request_with_retry(self.client, "GET", "/search/officers",
                                        params={"q": f"{first} {last}", "items_per_page": 50})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        numbers: set[str] = set()
        same_name = [i for i in resp.json().get("items", [])
                     if parse_officer_name(i.get("title", "")) == (first.title(), last.title())][:15]
        for item in same_name:
            link = (item.get("links") or {}).get("self")
            if not link:
                continue
            appts = await request_with_retry(self.client, "GET", link, params={"items_per_page": 50})
            if appts.status_code != 200:
                continue
            for a in appts.json().get("items", []):
                company = a.get("appointed_to") or {}
                words = set(normalize_company_name(company.get("company_name", "")).split())
                if (not a.get("resigned_on") and company.get("company_status") == "active"
                        and company.get("company_number") and words & brand_words):
                    numbers.add(company["company_number"])
        if len(numbers) != 1:
            return None  # none, or ambiguous: never guess
        return await self.profile(numbers.pop())

    async def decision_officers(self, number: str) -> list[Officer]:
        resp = await request_with_retry(self.client, "GET", f"/company/{number}/officers", params={"items_per_page": 50})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        out = []
        for item in resp.json().get("items", []):
            if item.get("resigned_on") or item.get("officer_role") not in DECISION_ROLES:
                continue
            first, last = parse_officer_name(item.get("name", ""))
            if first and last:
                out.append(Officer(first, last, item["officer_role"], _parse_date(item.get("appointed_on"))))
        return out
