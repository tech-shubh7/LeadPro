"""LinkedIn's public (logged-out) job search: job ads with the employer named and an exact posting date.

GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=&location=&f_TPR=r{secs}&start=N
    -> ~10 job cards of HTML per page; each card carries the job id, title, company (with its LinkedIn
       slug), location and <time datetime="YYYY-MM-DD">, the posting date.
GET https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}  -> the ad's full description.

These are the pages anyone sees without an account; no login, cookies or profile data are touched, and the
client identifies itself honestly. Use is the operator's decision (LinkedIn's terms restrict automated
access), so it can be switched off with LINKEDIN_JOBS_ENABLED=false. Requests go one at a time with a pause,
and a 429 stops LinkedIn for the rest of the run instead of retrying into a block. Checked live 2026-11-17.
"""

import asyncio
import re
from datetime import date
from urllib.parse import urlparse

import httpx
from lxml import html as lxml_html

from app.providers.jobs.job_ad import JobAd
from app.utils.logger import get_logger

logger = get_logger(__name__)
SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
PAGE_SIZE = 10
MAX_PAGES = 5
MAX_TEXT_CHARS = 2500
_JOB_ID = re.compile(r"jobPosting:(\d+)")


class LinkedInRateLimitedError(Exception):
    """LinkedIn answered 429: stop asking for this run."""


def _text(node, xpath: str) -> str:
    return " ".join(" ".join(node.xpath(xpath)).split())


def _company_slug(href: str) -> str:
    parts = [p for p in urlparse(href).path.split("/") if p]
    return parts[1] if len(parts) >= 2 and parts[0] == "company" else ""


def _iso_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def parse_cards(page: str) -> list[JobAd]:
    """Job cards from one search page. A card missing its id, title, company or date is skipped: an
    undated ad cannot be a current sign, and 'X days ago' text is never turned into a date."""
    if not page.strip():
        return []
    tree = lxml_html.fromstring(page)
    out = []
    for card in tree.xpath('//div[@data-entity-urn][contains(@class, "job-search-card")]'):
        job_id = _JOB_ID.search(card.get("data-entity-urn", ""))
        title = _text(card, './/h3[contains(@class, "base-search-card__title")]//text()')
        company = _text(card, './/h4[contains(@class, "base-search-card__subtitle")]//text()')
        posted = _iso_day(_text(card, ".//time/@datetime"))
        if not (job_id and title and company and posted):
            continue
        out.append(
            JobAd(
                job_id=job_id[1],
                employer_id=_company_slug(
                    _text(card, './/h4[contains(@class, "base-search-card__subtitle")]//a/@href')
                ),
                employer_name=company,
                title=title,
                location=_text(card, './/span[contains(@class, "job-search-card__location")]//text()'),
                posted_on=posted,
                description="",
                url=f"https://www.linkedin.com/jobs/view/{job_id[1]}",
                board="LinkedIn",
            )
        )
    return out


def parse_description(page: str) -> str:
    if not page.strip():
        return ""
    tree = lxml_html.fromstring(page)
    return _text(tree, '//div[contains(@class, "show-more-less-html__markup")]//text()')[:MAX_TEXT_CHARS]


class LinkedInJobsClient:
    def __init__(self, client: httpx.AsyncClient | None = None, pause_s: float = 2.5):
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5), headers={"User-Agent": "LeadPro/0.1 (+research)"}
        )
        self.pause_s = pause_s
        self._lock = asyncio.Lock()  # one request at a time, spaced out
        self.blocked = False

    async def _get(self, url: str, params: dict | None = None) -> str:
        if self.blocked:
            raise LinkedInRateLimitedError("already rate-limited this run")
        async with self._lock:
            resp = await self.client.get(url, params=params)
            await asyncio.sleep(self.pause_s)
        if resp.status_code == 429:
            self.blocked = True
            raise LinkedInRateLimitedError("LinkedIn returned 429")
        if resp.status_code in (400, 404):
            return ""  # past the last page, or an ad taken down
        resp.raise_for_status()
        return resp.text

    async def search(self, keywords: str, location: str, days: int, pages: int = 2) -> list[JobAd]:
        """Ads posted in the last `days` days, newest pages first. Stops early at an empty page."""
        out: list[JobAd] = []
        for page in range(min(pages, MAX_PAGES)):
            cards = parse_cards(
                await self._get(
                    SEARCH_URL,
                    {
                        "keywords": keywords,
                        "location": location,
                        "f_TPR": f"r{max(1, days) * 86400}",
                        "start": page * PAGE_SIZE,
                    },
                )
            )
            out += cards
            if len(cards) < PAGE_SIZE:
                break
        return out

    async def describe(self, ad: JobAd) -> JobAd:
        """The ad with its description; unchanged if it cannot be read (rate limit propagates)."""
        try:
            text = parse_description(await self._get(DETAIL_URL.format(job_id=ad.job_id)))
        except httpx.HTTPError as exc:
            logger.info("linkedin: description of %s unavailable: %s", ad.job_id, type(exc).__name__)
            return ad
        return JobAd(**{**ad.__dict__, "description": text}) if text else ad

    async def aclose(self) -> None:
        await self.client.aclose()
