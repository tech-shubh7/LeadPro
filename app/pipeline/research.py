"""Step 2a: fetch a company's own pages live and collect raw contact data."""
import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from lxml import html as lxml_html

from app.core.fetcher import Fetcher, FetchResult, visible_text
from app.core.normalize import PhoneHit, extract_emails, extract_phones, normalize_domain
from app.pipeline.discover import Candidate
from app.providers.registry.companies_house import find_company_numbers, find_legal_names

MAX_SITE_PAGES = 8
MAX_SEED_ARTICLES = 3
PAGE_KEYWORDS = {
    "about": ("about", "who-we-are", "our-story", "company"),
    "team": ("team", "people", "leadership", "management", "our-people", "board", "meet"),
    "contact": ("contact",),
    "news": ("news", "press", "media", "blog", "insights", "updates"),
    "careers": ("careers", "jobs", "vacancies", "join-us", "work-with-us"),
    "legal": ("privacy", "terms", "legal", "imprint", "company-information"),
}


BLOCKED_STATUSES = frozenset({401, 403, 429, 503})


@dataclass
class ResearchBundle:
    domain: str
    homepage: FetchResult | None
    website_blocked: bool = False  # site exists but refuses automated access; researched from other sources
    target_name: str | None = None
    target_category: str | None = None
    pages: dict[str, FetchResult] = field(default_factory=dict)  # every fetched page (site + seed articles), ok only
    seed_hits: list = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[PhoneHit] = field(default_factory=list)
    company_numbers: list[str] = field(default_factory=list)
    legal_names: list[str] = field(default_factory=list)

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
    try:
        tree = lxml_html.fromstring(page_html)
    except (ValueError, lxml_html.etree.ParserError):
        return []
    best: dict[str, str] = {}
    for a in tree.xpath("//a[@href]"):
        url = urljoin(base_url, a.get("href").split("#")[0])
        if urlparse(url).scheme not in ("http", "https") or normalize_domain(url) != domain:
            continue
        hay = f"{urlparse(url).path} {a.text_content()}".lower()
        for category, words in PAGE_KEYWORDS.items():
            if category not in best and any(w in hay for w in words):
                best[category] = url
    return list(dict.fromkeys(best.values()))


async def company_news(serper, name: str, gl: str, n: int) -> list:
    """Targeted searches about one company (multi-source evidence beyond its own website)."""
    hits = []
    for q in [f'"{name}"'][:n]:
        for h in await serper.query(q, kind="news", gl=gl, num=10, tbs="qdr:y"):
            if name.lower() in f"{h.title} {h.snippet}".lower():
                hits.append(h)
    return hits[:5]


async def research(candidate: Candidate, fetcher: Fetcher, phone_region: str,
                   serper=None, gl: str = "uk", news_queries: int = 0) -> ResearchBundle:
    homepage = None
    attempts = []
    for url in (f"https://{candidate.domain}/", f"https://www.{candidate.domain}/"):
        homepage = await fetcher.fetch(url)
        attempts.append(homepage)
        if homepage.ok:
            break
    live = homepage if homepage and homepage.ok else None
    blocked = live is None and any(
        a.status in BLOCKED_STATUSES or a.blocked_reason == "disallowed by robots.txt" for a in attempts)
    bundle = ResearchBundle(candidate.domain, live, website_blocked=blocked, seed_hits=list(candidate.seed_hits),
                            target_name=candidate.name_hint, target_category=candidate.category)
    if live is None and not blocked:
        return bundle
    if serper is not None and news_queries and candidate.name_hint:
        known = {h.link for h in bundle.seed_hits}
        bundle.seed_hits += [h for h in await company_news(serper, candidate.name_hint, gl, news_queries) if h.link not in known]

    links: list[str] = []
    if live is not None:
        bundle.pages[live.final_url] = live
        links = pick_internal_links(live.html, live.final_url, candidate.domain)[:MAX_SITE_PAGES]
    seed_urls = list(dict.fromkeys(h.link for h in bundle.seed_hits))[:MAX_SEED_ARTICLES + 2]
    results = await asyncio.gather(*(fetcher.fetch(u) for u in dict.fromkeys(links + seed_urls)))
    for r in results:
        if r.ok:
            bundle.pages[r.final_url] = r

    site_pages = [p for p in bundle.pages.values() if normalize_domain(p.final_url) == candidate.domain]
    blob_text = "\n".join(visible_text(p.html) for p in site_pages)
    blob_html = "\n".join(p.html for p in site_pages)
    bundle.emails = extract_emails(blob_text + "\n" + re.sub(r"mailto:", " ", blob_html), candidate.domain)
    # Phones from visible text plus tel:/WhatsApp links only; raw HTML/scripts give false numbers.
    links_only = " ".join(re.findall(r'(?:href=["\'](?:tel:|https?://(?:wa\.me|api\.whatsapp\.com)[^"\']*))([^"\']*)', blob_html))
    bundle.phones = extract_phones(blob_text + "\n" + links_only + "\n" + " ".join(re.findall(r"wa\.me/\+?\d{8,15}", blob_html)), phone_region)
    bundle.company_numbers = find_company_numbers(blob_text)
    bundle.legal_names = find_legal_names(blob_text, candidate.domain)
    return bundle
