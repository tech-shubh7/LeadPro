"""UK Contracts Finder award notices (official open data, OCDS 1.1).

GET https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?publishedFrom&publishedTo&stages=award
returns up to 100 releases a page and a cursor in `links.next`. Each award names the winning supplier, usually
with its Companies House number (parties[].identifier, scheme GB-COH), the buyer, value, CPV class and the award
date. Checked live 2026-11-01. A supplier without a GB-COH identifier is still returned, number unset.
"""

from dataclasses import dataclass
from datetime import date, datetime

import httpx

from app.utils.http_retry import request_with_retry

BASE = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"


@dataclass(frozen=True)
class Award:
    notice_url: str
    supplier_name: str
    supplier_number: str | None  # Companies House number, from the notice itself
    scale: str | None  # 'sme' | 'large' | None, as declared in the notice
    buyer: str
    title: str
    description: str
    cpv: str
    amount: float | None
    awarded_on: date | None  # when the contract was awarded (the event)
    published_on: date  # when the notice was published (trusted date)

    @property
    def text(self) -> str:
        value = f" Value: £{self.amount:,.0f}." if self.amount else ""
        when = f" Award date: {self.awarded_on.strftime('%d %B %Y').lstrip('0')}." if self.awarded_on else ""
        return (
            f"Contract award notice. {self.supplier_name} was awarded the contract '{self.title}' by {self.buyer}."
            f"{when}{value} {self.description}"
        ).strip()


def _day(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date() if value else None
    except ValueError:
        return None


def parse_release(release: dict) -> list[Award]:
    parties = {p.get("id"): p for p in release.get("parties", [])}
    buyer = (release.get("buyer") or {}).get("name", "")
    tender = release.get("tender") or {}
    out = []
    for aw in release.get("awards", []):
        notice = next(
            (d["url"] for d in aw.get("documents", []) if d.get("documentType") == "awardNotice"), None
        )
        published = _day(aw.get("datePublished")) or _day(release.get("date"))
        if not notice or not published:
            continue
        for sup in aw.get("suppliers", []):
            party = parties.get(sup.get("id"), {})
            ident = party.get("identifier") or {}
            number = ident.get("id") if ident.get("scheme") == "GB-COH" else None
            out.append(
                Award(
                    notice_url=notice,
                    supplier_name=sup.get("name") or party.get("name", ""),
                    supplier_number=number.zfill(8) if number else None,
                    scale=(party.get("details") or {}).get("scale"),
                    buyer=buyer,
                    title=tender.get("title", ""),
                    description=" ".join((tender.get("description") or "").split())[:1200],
                    cpv=(tender.get("classification") or {}).get("id", ""),
                    amount=(aw.get("value") or {}).get("amount"),
                    awarded_on=_day(aw.get("date")),
                    published_on=published,
                )
            )
    return [a for a in out if a.supplier_name]


class ContractsFinderClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(
            timeout=60, headers={"User-Agent": "LeadPro/0.1 (+research)"}
        )

    async def awards(self, published_from: date, published_to: date, max_pages: int = 10) -> list[Award]:
        url: str | None = BASE
        params: dict | None = {
            "publishedFrom": published_from.isoformat(),
            "publishedTo": published_to.isoformat(),
            "stages": "award",
        }
        out: list[Award] = []
        for _ in range(max_pages):
            resp = await request_with_retry(self.client, "GET", url, params=params)
            resp.raise_for_status()
            body = resp.json()
            for release in body.get("releases", []):
                out += parse_release(release)
            url, params = (
                (body.get("links") or {}).get("next"),
                None,
            )  # the cursor URL already carries its params
            if not url:
                break
        return out
