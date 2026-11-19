"""Step 2a: fetch a company's own pages live and collect raw contact data."""

import asyncio
import re
from dataclasses import dataclass, field, replace
from urllib.parse import urljoin, urlparse

from lxml import html as lxml_html

from app.providers.registry.companies_house_client import find_company_numbers, find_legal_names
from app.providers.web.page_fetcher import Fetcher, FetchResult, visible_text
from app.services.discovery_service import Candidate
from app.services.source_service import FIRST_PARTY_KINDS
from app.utils.normalize import PhoneHit, extract_emails, extract_phones, normalize_domain

MAX_SITE_PAGES = 8
MAX_SEED_PAGES = 5  # outside articles (seed/search/news hits) fetched per company
PAGE_KEYWORDS = {
    "about": ("about", "who-we-are", "our-story", "company"),
    "team": ("team", "people", "leadership", "management", "our-people", "board", "meet"),
    "contact": ("contact",),
    "news": ("news", "press", "media", "blog", "insights", "updates"),
    "careers": ("careers", "jobs", "vacancies", "join-us", "work-with-us"),
    "legal": ("privacy", "terms", "legal", "imprint", "company-information"),
}


BLOCKED_STATUSES = frozenset({401, 403, 429, 503})
# Pages only worth reading for people: not fetched when the run delivers no contacts (signals-only).
NOT_FETCHED_WITHOUT_PEOPLE = frozenset({"team"})


@dataclass
class ResearchBundle:
    domain: str
    homepage: FetchResult | None
    website_blocked: bool = False  # site exists but refuses automated access; researched from other sources
    target_name: str | None = None
    target_category: str | None = None
    pages: dict[str, FetchResult] = field(
        default_factory=dict
    )  # every fetched page (site + seed articles), ok only
    seed_hits: list = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[PhoneHit] = field(default_factory=list)
    company_numbers: list[str] = field(default_factory=list)
    legal_names: list[str] = field(default_factory=list)
    # Fetched site page (final URL) -> what kind of page it is (a PAGE_KEYWORDS key). Decides how much of
    # it the model reads: a privacy policy is parsed for company numbers in code, never sent to the model.
    page_roles: dict[str, str] = field(default_factory=dict)

    @property
    def website_live(self) -> bool:
        return self.homepage is not None and self.homepage.ok

    @property
    def access_note(self) -> str:
        if self.website_live:
            return "company website read"
        if self.website_blocked:
            return "company website not readable by automated tools; researched from news, registry and search results"
        return "company website unreachable"


def pick_internal_links(page_html: str, base_url: str, domain: str) -> list[str]:
    return list(dict.fromkeys(internal_link_roles(page_html, base_url, domain).values()))


def internal_link_roles(page_html: str, base_url: str, domain: str) -> dict[str, str]:
    """The best same-site link for each kind of page (role -> url), in the order found."""
    try:
        tree = lxml_html.fromstring(page_html)
    except (ValueError, lxml_html.etree.ParserError):
        return {}
    best: dict[str, str] = {}
    for a in tree.xpath("//a[@href]"):
        url = urljoin(base_url, a.get("href").split("#")[0])
        if urlparse(url).scheme not in ("http", "https") or normalize_domain(url) != domain:
            continue
        hay = f"{urlparse(url).path} {a.text_content()}".lower()
        for category, words in PAGE_KEYWORDS.items():
            if category not in best and any(w in hay for w in words):
                best[category] = url
    return best


async def company_news(serper, name: str, gl: str, n: int, tbs: str = "qdr:y") -> list:
    """Targeted searches about one company (multi-source evidence beyond its own website). Links are the
    real pages, not Google redirects: they are fetched, quoted and shown to the buyer as the source."""
    hits = []
    for q in [f'"{name}"'][:n]:
        for h in await serper.query(q, kind="news", gl=gl, num=10, tbs=tbs):
            if name.lower() in f"{h.title} {h.snippet}".lower():
                hits.append(h)
    hits = hits[:5]
    links = await asyncio.gather(*(serper.resolve_redirect(h.link) for h in hits))
    return [replace(h, link=link) for h, link in zip(hits, links, strict=True)]


async def research(
    candidate: Candidate,
    fetcher: Fetcher,
    phone_region: str,
    serper=None,
    gl: str = "uk",
    news_queries: int = 0,
    wants_people: bool = True,
    news_tbs: str = "qdr:y",
) -> ResearchBundle:
    homepage = None
    attempts = []
    for url in (f"https://{candidate.domain}/", f"https://www.{candidate.domain}/"):
        homepage = await fetcher.fetch(url)
        attempts.append(homepage)
        if homepage.ok:
            break
    live = homepage if homepage and homepage.ok else None
    blocked = live is None and any(
        a.status in BLOCKED_STATUSES or a.blocked_reason == "disallowed by robots.txt" for a in attempts
    )
    bundle = ResearchBundle(
        candidate.domain,
        live,
        website_blocked=blocked,
        seed_hits=list(candidate.seed_hits),
        target_name=candidate.name_hint,
        target_category=candidate.category,
    )
    if live is None and not blocked:
        return bundle
    if serper is not None and news_queries and candidate.name_hint:
        known = {h.link for h in bundle.seed_hits}
        bundle.seed_hits += [
            h
            for h in await company_news(serper, candidate.name_hint, gl, news_queries, news_tbs)
            if h.link not in known
        ]

    roles: dict[str, str] = {}  # requested url -> role
    if live is not None:
        bundle.pages[live.final_url] = live
        bundle.page_roles[live.final_url] = "home"
        found = internal_link_roles(live.html, live.final_url, candidate.domain)
        for role, url in list(found.items())[:MAX_SITE_PAGES]:
            if wants_people or role not in NOT_FETCHED_WITHOUT_PEOPLE:
                roles.setdefault(url, role)
    seed_urls = list(dict.fromkeys(h.link for h in bundle.seed_hits if h.kind not in FIRST_PARTY_KINDS))[
        :MAX_SEED_PAGES
    ]
    urls = list(dict.fromkeys([*roles, *seed_urls]))
    results = await asyncio.gather(*(fetcher.fetch(u) for u in urls))
    for url, r in zip(urls, results, strict=True):
        if r.ok:
            bundle.pages[r.final_url] = r
            if url in roles:
                bundle.page_roles.setdefault(r.final_url, roles[url])

    site_html = [p.html for p in bundle.pages.values() if normalize_domain(p.final_url) == candidate.domain]
    # HTML parsing and phone matching are CPU-bound: off the event loop, like page text extraction.
    bundle.emails, bundle.phones, bundle.company_numbers, bundle.legal_names = await asyncio.to_thread(
        contact_data, site_html, candidate.domain, phone_region
    )
    return bundle


def contact_data(site_html: list[str], domain: str, phone_region: str):
    """(emails, phones, company numbers, legal names) printed on the company's own pages."""
    blob_text = "\n".join(visible_text(h) for h in site_html)
    blob_html = "\n".join(site_html)
    emails = extract_emails(blob_text + "\n" + re.sub(r"mailto:", " ", blob_html), domain)
    # Phones from visible text plus tel:/WhatsApp links only; raw HTML/scripts give false numbers.
    links_only = " ".join(
        re.findall(
            r'(?:href=["\'](?:tel:|https?://(?:wa\.me|api\.whatsapp\.com)[^"\']*))([^"\']*)', blob_html
        )
    )
    phones = extract_phones(
        blob_text + "\n" + links_only + "\n" + " ".join(re.findall(r"wa\.me/\+?\d{8,15}", blob_html)),
        phone_region,
    )
    return emails, phones, find_company_numbers(blob_text), find_legal_names(blob_text, domain)
