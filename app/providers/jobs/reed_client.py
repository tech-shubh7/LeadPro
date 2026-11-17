"""Reed.co.uk Jobseeker API: UK job ads with the employer named, read through the official API.

GET https://www.reed.co.uk/api/1.0/search?keywords=&locationName=&contract=true&postedByDirectEmployer=true
    &resultsToTake=100&resultsToSkip=   (HTTP basic auth: API key as username, empty password)
GET https://www.reed.co.uk/api/1.0/jobs/{jobId}

Reed's docs list the fields but not their exact casing or the date format, so parsing accepts the likely
forms (jobId / JobId, dd/mm/yyyy / ISO) and skips a result missing what we need rather than guessing.
`postedByDirectEmployer` keeps recruitment agencies out at the source; ads that still read like a
recruiter ("our client is...") are filtered again by the caller.
"""

import re
from datetime import UTC, date, datetime

import httpx

from app.config.settings import get_settings
from app.providers.jobs.job_ad import JobAd
from app.utils.http_retry import request_with_retry
from app.utils.logger import get_logger

logger = get_logger(__name__)
BASE_URL = "https://www.reed.co.uk/api/1.0"
PAGE_SIZE = 100  # Reed's maximum per request


ReedJob = JobAd  # Reed results are plain job ads


def _field(item: dict, *names: str):
    for name in names:
        for variant in (name, name[:1].upper() + name[1:]):
            if item.get(variant) not in (None, ""):
                return item[variant]
    return None


def parse_reed_date(value) -> date | None:
    """dd/mm/yyyy (Reed's site format), ISO dates or datetimes, or .NET '/Date(ms)/'; None if unreadable."""
    if not value:
        return None
    text = str(value).strip()
    if match := re.search(r"/Date\((\d+)", text):
        return datetime.fromtimestamp(int(match[1]) / 1000, UTC).date()
    day = re.split(r"[ T]", text, maxsplit=1)[0]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(day, fmt).date()
        except ValueError:
            continue
    return None


def parse_job(item: dict) -> ReedJob | None:
    job_id = _field(item, "jobId")
    employer = _field(item, "employerName")
    title = _field(item, "jobTitle")
    if not (job_id and employer and title):
        return None
    url = _field(item, "jobUrl", "url") or f"https://www.reed.co.uk/jobs/{job_id}"
    return ReedJob(
        job_id=str(job_id),
        employer_id=str(_field(item, "employerId") or ""),
        employer_name=str(employer).strip(),
        title=str(title).strip(),
        location=str(_field(item, "locationName") or ""),
        posted_on=parse_reed_date(_field(item, "date", "datePosted")),
        description=" ".join(str(_field(item, "jobDescription", "description") or "").split()),
        url=str(url),
        board="Reed",
    )


class ReedClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        settings = get_settings()
        self.client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=45,
            auth=(settings.reed_api_key, ""),
            headers={"Accept": "application/json", "User-Agent": "LeadPro/0.1 (+research)"},
        )

    async def search(
        self,
        keywords: str,
        location: str = "",
        contract: bool = False,
        limit: int = PAGE_SIZE,
        direct_employer: bool = True,
    ) -> list[ReedJob]:
        params = {
            "keywords": keywords,
            "resultsToTake": min(limit, PAGE_SIZE),
            "postedByDirectEmployer": str(direct_employer).lower(),
        }
        if location:
            params["locationName"] = location
        if contract:
            params["contract"] = "true"
        resp = await request_with_retry(self.client, "GET", "/search", params=params)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        jobs = [job for item in results if (job := parse_job(item))]
        dropped = len(results) - len(jobs)
        if dropped:
            logger.warning("reed: %d result(s) without id, employer or title were skipped", dropped)
        return jobs

    async def aclose(self) -> None:
        await self.client.aclose()
