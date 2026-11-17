"""Structured-source discovery: public datasets in which every row is already a candidate buyer.

ats_jobs         companies with a live opening for a given kind of role (Greenhouse / Lever / Ashby /
                 SmartRecruiters public APIs). A posting is a dated, quotable signal by itself.
contracts_awards suppliers named in recent UK Contracts Finder award notices, with the Companies House
                 number the notice itself states.

Nothing here is trusted beyond "this company is worth researching": the website, quotes, dates, registry
match, scoring and verified email all run afterwards exactly as for any other candidate.
"""

import asyncio
import re
from datetime import date, timedelta

import httpx

from app.config.constants import STAFFING_WORDS
from app.config.settings import get_settings
from app.providers.jobs.ats_client import AtsClient, JobPosting, parse_board_url
from app.providers.jobs.reed_client import ReedClient
from app.providers.search.serper_client import SearchHit, SerperClient
from app.providers.tenders.contracts_finder_client import Award, ContractsFinderClient
from app.schemas.job_spec_schema import JobSpec, SourceSpec
from app.utils.dates import within_window
from app.utils.logger import get_logger
from app.utils.normalize import normalize_domain

logger = get_logger(__name__)
MAX_HITS_PER_COMPANY = 3
# Kinds of SearchHit whose text and date come straight from a first-party API response fetched in this run.
FIRST_PARTY_KINDS = frozenset({"ats", "job_ad", "notice"})
POSTING_KINDS = frozenset({"ats", "job_ad"})  # dated by the job board itself: a job posting


def _phrase(words: list[str]) -> re.Pattern | None:
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.I) if words else None


def ats_hit(p: JobPosting, today: date) -> SearchHit:
    where = f" ({p.location})" if p.location else ""
    dept = f" Team: {p.department}." if p.department else ""
    return SearchHit(
        title=f"{p.company_name} job posting: {p.title}{where}",
        link=p.url,
        snippet=f"Posted {p.posted_on.isoformat()} on {p.platform}.{dept} {p.text}".strip(),
        date_text=p.posted_on.isoformat(),
        kind="ats",
        query=f"ats:{p.platform}:{p.slug}",
        searched_on=today.isoformat(),
    )


def notice_hit(a: Award, today: date) -> SearchHit:
    return SearchHit(
        title=f"{a.supplier_name} awarded contract: {a.title}",
        link=a.notice_url,
        snippet=a.text,
        date_text=a.published_on.isoformat(),
        kind="notice",
        query="contracts_finder:award",
        searched_on=today.isoformat(),
    )


MAX_BOARD_QUERIES = 8
# Where each platform's boards are indexed. Greenhouse moved boards to job-boards.greenhouse.io; searches
# for the old host alone now return nothing (job 15).
BOARD_HOSTS = ("jobs.lever.co", "job-boards.greenhouse.io", "jobs.ashbyhq.com")


def board_queries(src: SourceSpec) -> list[str]:
    """The plan's board searches (old Greenhouse host widened to both), topped up with one search per
    platform and role so the source does not rest on two queries. A board is only a lead once its own API
    shows matching, dated, open roles, so a broad search costs a query, never a false lead."""
    out = [
        q.replace("site:boards.greenhouse.io", "(site:boards.greenhouse.io OR site:job-boards.greenhouse.io)")
        for q in src.search_queries
    ]
    where = f' "{src.locations[0]}"' if src.locations else ""
    for role in src.role_keywords[:2]:
        for host in BOARD_HOSTS:
            out.append(f'site:{host} "{role}"{where}')
    return list(dict.fromkeys(out))[:MAX_BOARD_QUERIES]


async def _ats_candidates(
    src: SourceSpec, spec: JobSpec, serper: SerperClient, ats: AtsClient, gl: str, today: date, budget: int
):
    from app.services.discovery_service import Candidate

    tbs = "qdr:m" if spec.freshness_days <= 45 else "qdr:y"
    boards: dict[tuple[str, str], None] = {}
    queries = board_queries(src)[: max(0, budget)]
    used = len(queries)
    for hits in await asyncio.gather(
        *(serper.query(q, kind="search", gl=gl, num=10, tbs=tbs) for q in queries)
    ):
        for hit in hits:
            if board := parse_board_url(hit.link):
                boards.setdefault(board)
    role, where = _phrase(src.role_keywords), _phrase(src.locations)
    out = []
    chosen = list(boards)[: src.max_boards]
    # All boards are fetched together; AtsClient's semaphore is what limits how many run at once.
    fetched = await asyncio.gather(*(ats.postings(platform, slug) for platform, slug in chosen))
    for postings in fetched:
        if len(postings) > src.max_open_roles:
            continue  # a large employer: hires in-house, not a buyer of outside delivery
        matching = [
            p
            for p in postings
            if within_window(p.posted_on, today, spec.freshness_days)
            and role.search(p.title)
            and (where is None or where.search(p.location))
        ]
        if len(matching) < src.min_roles:
            continue
        matching.sort(key=lambda p: p.posted_on, reverse=True)
        matching = (
            list(await asyncio.gather(*(ats.with_text(p) for p in matching[:MAX_HITS_PER_COMPANY])))
            + matching[MAX_HITS_PER_COMPANY:]
        )
        titles = "; ".join(f"{p.title} ({p.posted_on.isoformat()})" for p in matching[:MAX_HITS_PER_COMPANY])
        out.append(
            Candidate(
                domain="",
                name_hint=matching[0].company_name,
                category=src.category,
                prior=min(0.95, 0.65 + 0.1 * len(matching)),
                triage_note=f"{len(matching)} open matching role(s): {titles}",
                seed_hits=[ats_hit(p, today) for p in matching[:MAX_HITS_PER_COMPANY]],
            )
        )
    return out, used


async def _award_candidates(src: SourceSpec, cf: ContractsFinderClient, today: date):
    from app.services.discovery_service import Candidate

    awards = await cf.awards(today - timedelta(days=src.published_days), today, src.max_pages)
    kw, cpv = _phrase(src.keywords), tuple(src.cpv_prefixes)
    grouped: dict[str, list[Award]] = {}
    for a in awards:
        if src.sme_only and a.scale != "sme":
            continue
        if kw and not kw.search(f"{a.title} {a.description}") and not (cpv and a.cpv.startswith(cpv)):
            continue
        if not kw and cpv and not a.cpv.startswith(cpv):
            continue
        grouped.setdefault(a.supplier_number or a.supplier_name.lower(), []).append(a)
    out = []
    for group in grouped.values():
        group.sort(key=lambda a: a.published_on, reverse=True)
        head = group[0]
        out.append(
            Candidate(
                domain="",
                name_hint=head.supplier_name,
                category=src.category,
                prior=0.7,
                registry_id=head.supplier_number,
                triage_note=f"won {len(group)} matching contract(s), latest: {head.title} ({head.buyer})",
                seed_hits=[notice_hit(a, today) for a in group[:MAX_HITS_PER_COMPANY]],
            )
        )
    return out, 0


# Never handed to a browser agent: login walls, social networks and job aggregators (terms forbid it, and a
# row there names the platform, not a company we can research).
BROWSE_BLOCKED = frozenset(
    {
        "linkedin",
        "facebook",
        "instagram",
        "twitter",
        "x",
        "tiktok",
        "youtube",
        "reddit",
        "indeed",
        "glassdoor",
        "ziprecruiter",
        "monster",
        "totaljobs",
        "reed",
        "upwork",
        "fiverr",
        "freelancer",
        "peopleperhour",
    }
)


def browse_url_problem(url: str | None) -> str | None:
    """Why this page cannot be browsed, or None if it may be (robots.txt is checked at run time)."""
    if not url or not url.lower().startswith(("https://", "http://")):
        return "not a web address"
    domain = normalize_domain(url) or ""
    if any(label in BROWSE_BLOCKED for label in domain.split(".")[:-1]):
        return f"{domain} is a social network, login wall or job aggregator"
    return None


# An ad written by a recruiter for someone else's vacancy names the recruiter, not the company that needs
# the work. Reed's postedByDirectEmployer filter catches most; these phrases catch the rest.
_RECRUITER_WORDING = re.compile(
    r"\b(our client|my client|on behalf of|we are recruiting for|recruiting on behalf)\b", re.I
)
MAX_ADS_PER_SEARCH = 100


def _is_recruiter_ad(job) -> bool:
    name = job.employer_name.lower()
    return any(w in name for w in STAFFING_WORDS) or bool(_RECRUITER_WORDING.search(job.description))


async def _job_ad_candidates(src: SourceSpec, spec: JobSpec, reed, today: date) -> list:
    """Employers advertising contract/freelance roles on Reed, inside the freshness window, one candidate per
    employer with its ads as dated, first-party evidence. Signal first: every company found already has its
    reason, and nothing is spent researching companies without one."""
    from app.services.discovery_service import Candidate

    if reed is None:
        logger.warning("job-ad source skipped: no Reed client (REED_API_KEY)")
        return []
    locations = src.locations or [""]
    by_employer: dict[str, list] = {}
    for keywords in src.keywords:
        for location in locations:
            for job in await reed.search(
                keywords, location=location, contract=True, limit=MAX_ADS_PER_SEARCH
            ):
                if job.posted_on is None or not within_window(job.posted_on, today, spec.freshness_days):
                    continue  # undated or stale: not a current sign
                if _is_recruiter_ad(job):
                    continue
                by_employer.setdefault(job.employer_name.lower(), []).append(job)
    out = []
    for jobs in by_employer.values():
        jobs = sorted({j.job_id: j for j in jobs}.values(), key=lambda j: j.posted_on, reverse=True)
        first = jobs[0]
        titles = "; ".join(f"{j.title} ({j.posted_on.isoformat()})" for j in jobs[:MAX_HITS_PER_COMPANY])
        out.append(
            Candidate(
                domain="",
                name_hint=first.employer_name,
                category=src.category,
                prior=min(0.95, 0.7 + 0.05 * len(jobs)),
                triage_note=f"{len(jobs)} contract ad(s) on Reed: {titles}",
                seed_hits=[_job_ad_hit(j, today) for j in jobs[:MAX_HITS_PER_COMPANY]],
            )
        )
    return out


def _job_ad_hit(job, today: date) -> SearchHit:
    where = f" ({job.location})" if job.location else ""
    return SearchHit(
        title=f"{job.employer_name} job posting: {job.title}{where}",
        link=job.url,
        snippet=f"Posted {job.posted_on.isoformat()} on Reed. {job.description}".strip(),
        date_text=job.posted_on.isoformat(),
        kind="job_ad",
        query=f"reed:{job.employer_id or job.job_id}",
        searched_on=today.isoformat(),
    )


async def _browse_candidates(src: SourceSpec, agent) -> list:
    """Companies a browser agent lists from one public page. Discovery only: each becomes a candidate
    whose evidence page our own fetcher reads and quote-checks later."""
    from app.providers.web.page_fetcher import robots_allowed
    from app.services.discovery_service import Candidate

    if problem := browse_url_problem(src.url):
        logger.warning("browse source %s skipped: %s", src.url, problem)
        return []
    if not await robots_allowed(src.url):
        logger.warning("browse source %s skipped: disallowed by robots.txt", src.url)
        return []
    if agent is None:
        logger.warning("browse source %s skipped: no browser agent configured (TINYFISH_API_KEY)", src.url)
        return []
    rows = await agent.list_companies(src.url, src.goal, src.max_steps, src.max_companies)
    today = date.today().isoformat()
    out = []
    for row in rows:
        website = normalize_domain(row.company_website) if row.company_website else None
        hit = SearchHit(
            title=f"{row.company_name}: {row.evidence_text}"[:300],
            link=row.evidence_url,
            snippet=row.evidence_text,
            date_text=row.date or None,
            kind="search",
            query=f"browse:{src.url}",
            searched_on=today,
        )
        out.append(
            Candidate(
                domain=website or "",
                name_hint=row.company_name,
                category=src.category,
                prior=0.7,
                triage_note=f"listed on {src.url}",
                seed_hits=[hit],
            )
        )
    return out


async def collect_sources(
    spec: JobSpec, serper: SerperClient, clients: dict | None, gl: str, today: date, budget: int
):
    """Returns (candidates, search-query budget used). `clients` may supply {'ats': AtsClient,
    'contracts': ContractsFinderClient} (tests); otherwise real ones are created and closed here."""
    clients = clients or {}
    ats = clients.get("ats") or AtsClient()
    cf = clients.get("contracts") or ContractsFinderClient()
    owns_reed = "reed" not in clients and bool(get_settings().reed_api_key)
    if owns_reed:
        clients = {**clients, "reed": ReedClient()}
    candidates, spent = [], 0
    try:
        for src in spec.sources:
            try:
                if src.type == "ats_jobs":
                    found, used = await _ats_candidates(src, spec, serper, ats, gl, today, budget - spent)
                elif src.type == "job_ads":
                    found, used = await _job_ad_candidates(src, spec, clients.get("reed"), today), 0
                elif src.type == "agent_browse":
                    found, used = await _browse_candidates(src, clients.get("agent")), 0
                else:
                    found, used = await _award_candidates(src, cf, today)
            except (httpx.HTTPError, ValueError) as exc:
                # A public dataset being down (or returning junk) loses that source only, not the whole job.
                logger.warning("source %s failed, skipped: %s: %s", src.type, type(exc).__name__, exc)
                continue
            candidates += found
            spent += used
    finally:
        if "ats" not in clients:
            await ats.client.aclose()
        if "contracts" not in clients:
            await cf.client.aclose()
        if owns_reed:
            await clients["reed"].aclose()
    return candidates, spent


# Where each platform's boards live. A company's own site links to its board at one of these.
BOARD_URLS = {
    "greenhouse": (
        "boards.greenhouse.io/{slug}",
        "job-boards.greenhouse.io/{slug}",
        "greenhouse.io/embed/job_board?for={slug}",
    ),
    "lever": ("jobs.lever.co/{slug}",),
    "ashby": ("jobs.ashbyhq.com/{slug}",),
    "smartrecruiters": ("jobs.smartrecruiters.com/{slug}", "careers.smartrecruiters.com/{slug}"),
}


def _links_to_board(html: str, platform: str, slug: str) -> bool:
    page = html.lower()
    return any(pattern.format(slug=slug.lower()) in page for pattern in BOARD_URLS.get(platform, ()))


def board_checker_for(fetcher):
    """A checker for discovery: True if the site links to that board, False if it was read and does not,
    None if it could not be read (no proof either way). Two free page fetches at most, no model."""
    from app.services.research_service import pick_internal_links

    async def check(domain: str, platform: str, slug: str) -> bool | None:
        home = await fetcher.fetch(f"https://{domain}/")
        if not home.ok:
            return None
        if _links_to_board(home.html, platform, slug):
            return True
        careers = [
            u
            for u in pick_internal_links(home.html, home.final_url, domain)
            if any(w in u.lower() for w in ("career", "job", "vacanc", "join"))
        ]
        if careers:
            page = await fetcher.fetch(careers[0])
            if page.ok and _links_to_board(page.html, platform, slug):
                return True
        return False

    return check
