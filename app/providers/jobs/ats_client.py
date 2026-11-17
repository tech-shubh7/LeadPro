"""Public job-board APIs: Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Teamtailor.

These are the platforms' own documented, unauthenticated endpoints for publishing a company's open roles,
so reading them is intended use (no scraping, no bot protection involved). Endpoints were checked live
on 2026-11-01. The posting date is the platform's first-published date: Greenhouse `updated_at` is NOT
used because it changes on every edit and would make an old role look new.
Workable (careers widget), Recruitee (careers-site API) and Teamtailor (jobs RSS) were checked live on
2026-11-17; they are where many small agencies list roles, which the first four rarely carry.
"""

import asyncio
import contextlib
import html as html_lib
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import httpx
from lxml import etree
from lxml import html as lxml_html

from app.utils.http_retry import request_with_retry

PLATFORMS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "teamtailor")
# Subdomain-per-company platforms: labels that are the platform's own pages, never a company.
_RESERVED_SUBDOMAINS = frozenset(
    {"www", "app", "api", "career", "careers", "support", "help", "partners", "blog"}
)
_WORKABLE_RESERVED = frozenset({"j", "api", "jobs"})
_SLUG = re.compile(r"^[A-Za-z0-9_-]{2,60}$")
_GREENHOUSE_HOSTS = frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"})
MAX_TEXT_CHARS = 2500
BOARD_TIMEOUT_S = 40


@dataclass(frozen=True)
class JobPosting:
    platform: str
    slug: str
    company_name: (
        str  # exact from the platform where it publishes one (Greenhouse, SmartRecruiters); else the slug
    )
    title: str
    department: str
    location: str
    url: str
    posted_on: date
    text: str  # plain-text job description (what quotes are verified against)
    ref: str = (
        ""  # platform's own posting id (lets Greenhouse descriptions be fetched for matching postings only)
    )


def parse_board_url(url: str) -> tuple[str, str] | None:
    """('lever', 'acme') for https://jobs.lever.co/acme/..., else None."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host in _GREENHOUSE_HOSTS:
        platform = "greenhouse"
        if parts[:1] == ["embed"]:  # embedded widget links carry the company in ?for=
            parts = parse_qs(parsed.query).get("for", [])
    elif host == "jobs.lever.co":
        platform = "lever"
    elif host == "jobs.ashbyhq.com":
        platform = "ashby"
    elif host == "jobs.smartrecruiters.com":
        platform = "smartrecruiters"
    elif host == "apply.workable.com":
        platform = "workable"
        if parts[:1] and parts[0] in _WORKABLE_RESERVED:
            return None
    elif (sub := _company_subdomain(host, "recruitee.com")) or (
        sub := _company_subdomain(host, "teamtailor.com")
    ):
        platform = "recruitee" if host.endswith(".recruitee.com") else "teamtailor"
        parts = [sub]
    else:
        return None
    if not parts or not _SLUG.match(parts[0]):
        return None
    return platform, parts[0]


def _company_subdomain(host: str, base: str) -> str | None:
    """'acme' for acme.recruitee.com; None for the platform's own pages or deeper hosts."""
    if not host.endswith(f".{base}"):
        return None
    label = host.removesuffix(f".{base}")
    return None if "." in label or label in _RESERVED_SUBDOMAINS else label


def plain_text(markup: str) -> str:
    """Job descriptions arrive as HTML (Greenhouse: HTML-escaped HTML). Returns whitespace-collapsed text."""
    if not markup:
        return ""
    text = html_lib.unescape(markup)
    with contextlib.suppress(ValueError, lxml_html.etree.ParserError):
        text = lxml_html.fromstring(f"<div>{text}</div>").text_content()
    return re.sub(r"\s+", " ", text).strip()


def _day(value: str | int | float | None) -> date | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):  # Lever: epoch milliseconds
            return datetime.fromtimestamp(value / 1000, UTC).date()
        text = str(value).strip()
        if text.endswith(" UTC"):  # Recruitee: '2026-11-16 16:33:49 UTC'
            text = text.removesuffix(" UTC") + "+00:00"
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:  # Teamtailor RSS: 'Wed, 11 Nov 2026 17:34:36 +0100'
            return parsedate_to_datetime(text).date()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _name_from_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


class AtsClient:
    def __init__(self, client: httpx.AsyncClient | None = None, concurrency: int = 5):
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5), headers={"User-Agent": "LeadPro/0.1 (+research)"}
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._cache: dict[tuple[str, str], list[JobPosting]] = {}

    async def postings(self, platform: str, slug: str) -> list[JobPosting]:
        key = (platform, slug)
        if key not in self._cache:
            async with self._sem:
                try:  # one slow board must not hold up the batch: hard cap, then skipped (never guessed at)
                    self._cache[key] = await asyncio.wait_for(
                        getattr(self, f"_{platform}")(slug), BOARD_TIMEOUT_S
                    )
                except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError):
                    self._cache[key] = []  # an unreadable board is skipped, never guessed at
        return self._cache[key]

    async def _get_json(self, url: str, **params):
        resp = await request_with_retry(self.client, "GET", url, attempts=2, params=params or None)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def with_text(self, p: JobPosting) -> JobPosting:
        """The posting with its description filled in (only Greenhouse lists without one)."""
        if p.platform != "greenhouse" or p.text or not p.ref:
            return p
        async with self._sem:
            try:
                data = await self._get_json(
                    f"https://boards-api.greenhouse.io/v1/boards/{p.slug}/jobs/{p.ref}"
                )
            except (httpx.HTTPError, ValueError):
                return p
        data = data or {}
        dept = ", ".join(d.get("name", "") for d in data.get("departments") or [])
        return replace(p, department=dept, text=plain_text(data.get("content", ""))[:MAX_TEXT_CHARS])

    async def _greenhouse(self, slug: str) -> list[JobPosting]:
        # Listing only (no descriptions: big boards run to megabytes); `with_text` adds them for matches.
        data = await self._get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        out = []
        for j in (data or {}).get("jobs", []):
            if not (posted := _day(j.get("first_published"))):
                continue
            out.append(
                JobPosting(
                    "greenhouse",
                    slug,
                    j.get("company_name") or _name_from_slug(slug),
                    j["title"],
                    "",
                    (j.get("location") or {}).get("name", ""),
                    j["absolute_url"],
                    posted,
                    "",
                    ref=str(j["id"]),
                )
            )
        return out

    async def _lever(self, slug: str) -> list[JobPosting]:
        data = await self._get_json(f"https://api.lever.co/v0/postings/{slug}", mode="json")
        out = []
        for j in data or []:
            if not (posted := _day(j.get("createdAt"))):
                continue
            cat = j.get("categories") or {}
            out.append(
                JobPosting(
                    "lever",
                    slug,
                    _name_from_slug(slug),
                    j["text"],
                    cat.get("department") or cat.get("team") or "",
                    cat.get("location") or "",
                    j["hostedUrl"],
                    posted,
                    _squash(j.get("descriptionPlain"))[:MAX_TEXT_CHARS],
                )
            )
        return out

    async def _ashby(self, slug: str) -> list[JobPosting]:
        data = await self._get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        out = []
        for j in (data or {}).get("jobs", []):
            if not j.get("isListed", True) or not (posted := _day(j.get("publishedAt"))):
                continue
            out.append(
                JobPosting(
                    "ashby",
                    slug,
                    _name_from_slug(slug),
                    j["title"],
                    j.get("department") or "",
                    j.get("location") or "",
                    j["jobUrl"],
                    posted,
                    _squash(j.get("descriptionPlain"))[:MAX_TEXT_CHARS],
                )
            )
        return out

    async def _smartrecruiters(self, slug: str) -> list[JobPosting]:
        data = await self._get_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", limit=100
        )
        out = []
        for j in (data or {}).get("content", []):
            if not (posted := _day(j.get("releasedDate"))):
                continue
            loc = j.get("location") or {}
            where = ", ".join(x for x in (loc.get("city"), (loc.get("country") or "").upper()) if x)
            dept = (j.get("department") or {}).get("label", "")
            func = (j.get("function") or {}).get("label", "")
            out.append(
                JobPosting(
                    "smartrecruiters",
                    slug,
                    (j.get("company") or {}).get("name") or _name_from_slug(slug),
                    j["name"],
                    dept,
                    where,
                    f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                    posted,
                    f"{j['name']}. Department: {dept}. Function: {func}. Location: {where}.",
                )
            )
        return out

    async def _workable(self, slug: str) -> list[JobPosting]:
        data = await self._get_json(
            f"https://apply.workable.com/api/v1/widget/accounts/{slug}", details="true"
        )
        name = (data or {}).get("name") or _name_from_slug(slug)
        out = []
        for j in (data or {}).get("jobs", []):
            if not (posted := _day(j.get("published_on") or j.get("created_at"))):
                continue
            where = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
            out.append(
                JobPosting(
                    "workable",
                    slug,
                    name,
                    j["title"],
                    j.get("department") or "",
                    where,
                    j.get("url") or f"https://apply.workable.com/{slug}/j/{j['shortcode']}/",
                    posted,
                    plain_text(j.get("description", ""))[:MAX_TEXT_CHARS],
                )
            )
        return out

    async def _recruitee(self, slug: str) -> list[JobPosting]:
        data = await self._get_json(f"https://{slug}.recruitee.com/api/offers/")
        out = []
        for j in (data or {}).get("offers", []):
            if j.get("status", "published") != "published":
                continue
            if not (posted := _day(j.get("published_at") or j.get("created_at"))):
                continue
            text = plain_text(f"{j.get('description', '')} {j.get('requirements', '')}")
            out.append(
                JobPosting(
                    "recruitee",
                    slug,
                    j.get("company_name") or _name_from_slug(slug),
                    j["title"],
                    j.get("department") or "",
                    j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x),
                    j.get("careers_url") or f"https://{slug}.recruitee.com/o/{j.get('slug', '')}",
                    posted,
                    text[:MAX_TEXT_CHARS],
                )
            )
        return out

    async def _teamtailor(self, slug: str) -> list[JobPosting]:
        resp = await request_with_retry(
            self.client, "GET", f"https://{slug}.teamtailor.com/jobs.rss", attempts=2
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        # A third party's XML: no entity expansion, no network lookups.
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
        try:
            root = etree.fromstring(resp.content, parser)
        except etree.XMLSyntaxError as exc:
            raise ValueError("unreadable Teamtailor feed") from exc
        name = _squash(root.findtext("channel/title") or "") or _name_from_slug(slug)
        out = []
        for item in root.iterfind("channel/item"):
            if not (posted := _day(item.findtext("pubDate"))):
                continue
            city = " ".join(x.text or "" for x in item.iter() if etree.QName(x).localname == "city")
            country = " ".join(x.text or "" for x in item.iter() if etree.QName(x).localname == "country")
            out.append(
                JobPosting(
                    "teamtailor",
                    slug,
                    name,
                    _squash(item.findtext("title") or ""),
                    "",
                    ", ".join(x for x in (_squash(city), _squash(country)) if x),
                    _squash(item.findtext("link") or ""),
                    posted,
                    plain_text(item.findtext("description") or "")[:MAX_TEXT_CHARS],
                )
            )
        return [p for p in out if p.title and p.url]
