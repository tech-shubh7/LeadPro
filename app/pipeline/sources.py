"""Structured-source discovery: public datasets in which every row is already a candidate buyer.

ats_jobs         companies with a live opening for a given kind of role (Greenhouse / Lever / Ashby /
                 SmartRecruiters public APIs). A posting is a dated, quotable signal by itself.
contracts_awards suppliers named in recent UK Contracts Finder award notices, with the Companies House
                 number the notice itself states.

Nothing here is trusted beyond "this company is worth researching": the website, quotes, dates, registry
match, scoring and verified email all run afterwards exactly as for any other candidate.
"""
import asyncio
import logging
import re
from datetime import date, timedelta

import httpx

from app.core.dates import within_window
from app.pipeline.jobspec import JobSpec, SourceSpec
from app.providers.jobs.ats import AtsClient, JobPosting, parse_board_url
from app.providers.search.serper import SearchHit, SerperClient
from app.providers.tenders.contracts_finder import Award, ContractsFinderClient

log = logging.getLogger("leadpro")
MAX_HITS_PER_COMPANY = 3
# Kinds of SearchHit whose text and date come straight from a first-party API response fetched in this run.
FIRST_PARTY_KINDS = frozenset({"ats", "notice"})


def _phrase(words: list[str]) -> re.Pattern | None:
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.I) if words else None


def ats_hit(p: JobPosting, today: date) -> SearchHit:
    where = f" ({p.location})" if p.location else ""
    dept = f" Team: {p.department}." if p.department else ""
    return SearchHit(title=f"{p.company_name} job posting: {p.title}{where}", link=p.url,
                     snippet=f"Posted {p.posted_on.isoformat()} on {p.platform}.{dept} {p.text}".strip(),
                     date_text=p.posted_on.isoformat(), kind="ats", query=f"ats:{p.platform}:{p.slug}",
                     searched_on=today.isoformat())


def notice_hit(a: Award, today: date) -> SearchHit:
    return SearchHit(title=f"{a.supplier_name} awarded contract: {a.title}", link=a.notice_url, snippet=a.text,
                     date_text=a.published_on.isoformat(), kind="notice", query="contracts_finder:award",
                     searched_on=today.isoformat())


MAX_BOARD_QUERIES = 8
# Where each platform's boards are indexed. Greenhouse moved boards to job-boards.greenhouse.io; searches
# for the old host alone now return nothing (job 15).
BOARD_HOSTS = ("jobs.lever.co", "job-boards.greenhouse.io", "jobs.ashbyhq.com")


def board_queries(src: SourceSpec) -> list[str]:
    """The plan's board searches (old Greenhouse host widened to both), topped up with one search per
    platform and role so the source does not rest on two queries. A board is only a lead once its own API
    shows matching, dated, open roles, so a broad search costs a query, never a false lead."""
    out = [q.replace("site:boards.greenhouse.io", "(site:boards.greenhouse.io OR site:job-boards.greenhouse.io)")
           for q in src.search_queries]
    where = f' "{src.locations[0]}"' if src.locations else ""
    for role in src.role_keywords[:2]:
        for host in BOARD_HOSTS:
            out.append(f'site:{host} "{role}"{where}')
    return list(dict.fromkeys(out))[:MAX_BOARD_QUERIES]


async def _ats_candidates(src: SourceSpec, spec: JobSpec, serper: SerperClient, ats: AtsClient, gl: str,
                          today: date, budget: int):
    from app.pipeline.discover import Candidate
    tbs = "qdr:m" if spec.freshness_days <= 45 else "qdr:y"
    boards: dict[tuple[str, str], None] = {}
    queries = board_queries(src)[:max(0, budget)]
    used = len(queries)
    for hits in await asyncio.gather(*(serper.query(q, kind="search", gl=gl, num=10, tbs=tbs) for q in queries)):
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
        matching = [p for p in postings
                    if within_window(p.posted_on, today, spec.freshness_days) and role.search(p.title)
                    and (where is None or where.search(p.location))]
        if len(matching) < src.min_roles:
            continue
        matching.sort(key=lambda p: p.posted_on, reverse=True)
        matching = list(await asyncio.gather(*(ats.with_text(p) for p in matching[:MAX_HITS_PER_COMPANY]))) + matching[MAX_HITS_PER_COMPANY:]
        titles = "; ".join(f"{p.title} ({p.posted_on.isoformat()})" for p in matching[:MAX_HITS_PER_COMPANY])
        out.append(Candidate(domain="", name_hint=matching[0].company_name, category=src.category,
                             prior=min(0.95, 0.65 + 0.1 * len(matching)),
                             triage_note=f"{len(matching)} open matching role(s): {titles}",
                             seed_hits=[ats_hit(p, today) for p in matching[:MAX_HITS_PER_COMPANY]]))
    return out, used


async def _award_candidates(src: SourceSpec, cf: ContractsFinderClient, today: date):
    from app.pipeline.discover import Candidate
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
        out.append(Candidate(domain="", name_hint=head.supplier_name, category=src.category, prior=0.7,
                             registry_id=head.supplier_number,
                             triage_note=f"won {len(group)} matching contract(s), latest: {head.title} ({head.buyer})",
                             seed_hits=[notice_hit(a, today) for a in group[:MAX_HITS_PER_COMPANY]]))
    return out, 0


async def collect_sources(spec: JobSpec, serper: SerperClient, clients: dict | None, gl: str, today: date, budget: int):
    """Returns (candidates, search-query budget used). `clients` may supply {'ats': AtsClient,
    'contracts': ContractsFinderClient} (tests); otherwise real ones are created and closed here."""
    clients = clients or {}
    ats = clients.get("ats") or AtsClient()
    cf = clients.get("contracts") or ContractsFinderClient()
    candidates, spent = [], 0
    try:
        for src in spec.sources:
            try:
                if src.type == "ats_jobs":
                    found, used = await _ats_candidates(src, spec, serper, ats, gl, today, budget - spent)
                else:
                    found, used = await _award_candidates(src, cf, today)
            except (httpx.HTTPError, ValueError) as exc:
                # A public dataset being down (or returning junk) loses that source only, not the whole job.
                log.warning("source %s failed, skipped: %s: %s", src.type, type(exc).__name__, exc)
                continue
            candidates += found
            spent += used
    finally:
        if "ats" not in clients:
            await ats.client.aclose()
        if "contracts" not in clients:
            await cf.client.aclose()
    return candidates, spent
