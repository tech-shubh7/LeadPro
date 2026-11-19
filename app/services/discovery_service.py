"""Step 1: agentic discovery.

Rounds of: plan queries (judge model) -> search -> triage every hit in one batch (cheap model) ->
resolve the companies' own domains -> rank. After each round the planner sees how productive each
query was and writes better ones, until enough strong candidates exist or the query budget is spent.
Nothing is fetched or deeply researched until a hit has been triaged as a likely target company.
"""

import asyncio
import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.config.constants import COUNTRIES, WORLDWIDE
from app.config.settings import get_settings
from app.exceptions.custom_exceptions import LLMRefusalError
from app.providers.llm.llm_provider import LLMProvider
from app.providers.search.serper_client import SearchHit, SerperClient
from app.schemas.job_spec_schema import JobSpec
from app.services.source_service import collect_sources
from app.utils.dates import search_window
from app.utils.logger import get_logger
from app.utils.normalize import normalize_domain

# Domains that host content about *other* companies (news, job boards, directories, social).
# Brokers and directories whose brand is an ordinary word a real company could be named after: blocked by
# exact domain only, so an agency called "Craft" or "Apollo" is never mistaken for one.
_AMBIGUOUS_THIRD_PARTY = frozenset({"craft.co", "seamless.ai", "apollo.io", "bark.com", "g2.com"})
THIRD_PARTY_DOMAINS = frozenset(
    {
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "tiktok.com",
        "indeed.com",
        "indeed.co.uk",
        "reed.co.uk",
        "totaljobs.com",
        "glassdoor.com",
        "glassdoor.co.uk",
        "cv-library.co.uk",
        "monster.co.uk",
        "jobsite.co.uk",
        "adzuna.co.uk",
        "ziprecruiter.co.uk",
        "jobleads.com",
        "simplyhired.co.uk",
        "simplyhired.com",
        "dealroom.co",
        "dayforcehcm.com",
        "workable.com",
        "greenhouse.io",
        "lever.co",
        "bamboohr.com",
        "teamtailor.com",
        "recruitee.com",
        "personio.de",
        "breezy.hr",
        "smartrecruiters.com",
        "bbc.co.uk",
        "theguardian.com",
        "telegraph.co.uk",
        "ft.com",
        "thetimes.co.uk",
        "thetimes.com",
        "independent.co.uk",
        "cityam.com",
        "insidermedia.com",
        "businesslive.co.uk",
        "prolificnorth.co.uk",
        "uktech.news",
        "reuters.com",
        "bloomberg.com",
        "forbes.com",
        "businessoffashion.com",
        "retail-gazette.co.uk",
        "drapersonline.com",
        "thegrocer.co.uk",
        "cosmeticsbusiness.com",
        "globalcosmeticsnews.com",
        "eu-startups.com",
        "tech.eu",
        "startups.co.uk",
        "yahoo.com",
        "msn.com",
        "dailymail.co.uk",
        "standard.co.uk",
        "finsmes.com",
        "fiercehealthcare.com",
        "techcrunch.com",
        "sifted.eu",
        "crunchbase.com",
        "pitchbook.com",
        "tracxn.com",
        "cbinsights.com",
        "companieshouse.gov.uk",
        "gov.uk",
        "endole.co.uk",
        "companycheck.co.uk",
        "opencorporates.com",
        "yell.com",
        "wikipedia.org",
        "amazon.co.uk",
        "amazon.com",
        "ebay.co.uk",
        "trustpilot.com",
        "medium.com",
        "reddit.com",
        "prnewswire.com",
        "businesswire.com",
        "globenewswire.com",
        "google.com",
        "apple.com",
        "studysmarter.co.uk",
        # job aggregators / career-advice and company-listing sites (never a company's own website)
        "expertini.com",
        "jobgether.com",
        "talent.com",
        "jooble.org",
        "careerjet.co.uk",
        "jobrapido.com",
        "neuvoo.co.uk",
        "tealhq.com",
        "wfh.team",
        "flowremote.io",
        "jobs.workable.com",
        "huntukvisasponsors.com",
        "justlondonjobs.com",
        "leicesterjobs.uk",
        "cwjobs.co.uk",
        "jobserve.com",
        "talents.studysmarter.co.uk",
        "digitalagencynetwork.com",
        "designrush.com",
        "clutch.co",
        "goodfirms.co",
        "selectedfirms.co",
        "techbehemoths.com",
        "sortlist.com",
        "getlatka.com",
        "zoominfo.com",
        "rocketreach.co",
        "apollo.io",
        "lusha.com",
        "dnb.com",
        "uk.indeed.com",
        "lbbonline.com",
        "businesscloud.co.uk",
        "thebusinessdesk.com",
        "bdaily.co.uk",
        "finance.yahoo.com",
    }
)
logger = get_logger(__name__)
# Google's `gl` (results country). There is no worldwide setting: without one Google answers as for "us",
# its broadest English index, so a worldwide run says so explicitly instead of falling back to "uk".
GL = {"GB": "uk", "US": "us", "IN": "in", WORLDWIDE: "us"}


# Brands that host pages about other companies under many TLDs and subdomains (glassdoor.be,
# uk.indeed.com, jobs.ashbyhq.com). Matched on any label of the domain, not the exact host, so a new
# country site cannot pass for a company's own website. Only names that are never a buyer's own brand.
THIRD_PARTY_BRANDS = frozenset(
    {
        # people and company data brokers: pages ABOUT a company, never its own site (job 25)
        "theorg",
        "signalhire",
        "rocketreach",
        "zoominfo",
        "lusha",
        "kaspr",
        "contactout",
        "leadiq",
        "crunchbase",
        "pitchbook",
        "owler",
        "companieshouse",
        "endole",
        "opencorporates",
        # agency and business directories, review and listing sites
        "agencies",
        "clutch",
        "designrush",
        "sortlist",
        "goodfirms",
        "themanifest",
        "upcity",
        "yell",
        "checkatrade",
        "trustpilot",
        "cylex",
        "hotfrog",
        "brownbook",
        "yelp",
        "capterra",
        "indeed",
        "glassdoor",
        "ziprecruiter",
        "monster",
        "linkedin",
        "simplyhired",
        "adzuna",
        "jooble",
        "careerjet",
        "totaljobs",
        "jobrapido",
        "neuvoo",
        "jobgether",
        "expertini",
        "ashbyhq",
        "greenhouse",
        "smartrecruiters",
        "recruitee",
        "teamtailor",
        "personio",
        "bamboohr",
        "jobvite",
        "icims",
        "myworkdayjobs",
        "workable",
        "breezy",
        "remoteok",
        "weworkremotely",
        "nodesk",
        "remotive",
        "wellfound",
        "himalayas",
        "workingnomads",
        "builtin",
        "otta",
        "wikipedia",
        "facebook",
        "instagram",
        "youtube",
        "tiktok",
        "imdb",
    }
)
# Job boards nearly always carry "jobs" in their name (gamesjobs, cwjobs, justlondonjobs, jobsite).
_JOB_BOARD_LABEL = re.compile(r"^(?:jobs?[a-z0-9-]*|[a-z0-9-]*jobs)$")
# A search title like "Temporal jobs - ZipRecruiter" names the company but the page is a listing about it.
LISTING_WORDS = re.compile(
    r"\b(jobs?|careers? at|hiring|vacanc(?:y|ies)|salar(?:y|ies)|reviews?|job openings)\b", re.I
)


_PUBLIC_BODY = re.compile(
    r"(^|\.)(gov|mil|edu|nhs|police|ac)(\.[a-z]{2})?$"
)  # .gov, .gov.uk, .ac.uk, .edu ...


def is_third_party(domain: str) -> bool:
    if domain in THIRD_PARTY_DOMAINS or domain in _AMBIGUOUS_THIRD_PARTY or _PUBLIC_BODY.search(domain):
        return True
    labels = domain.split(".")[:-1]
    return any(label in THIRD_PARTY_BRANDS or _JOB_BOARD_LABEL.match(label) for label in labels)


@dataclass
class Candidate:
    domain: str
    name_hint: str | None = None
    seed_hits: list[SearchHit] = field(default_factory=list)
    prior: float = 0.0  # triage fit estimate 0-1, used to research the most promising first
    triage_note: str = ""
    category: str | None = None  # e.g. "functional coffee brand"; disambiguates common-word names
    registry_id: str | None = (
        None  # company number stated by the discovery source itself (e.g. a contract award notice)
    )
    seeded: bool = False  # nominated in the job spec by outside research, not found by discovery


# Where a candidate came from, for the source performance report. Read from the hit that brought the
# candidate in (hits record their query: "ats:lever:acme", "linkedin:123", "reed:456", "browse:<url>"),
# so no discovery step has to remember it separately.
CHANNEL_SEED = "seed"
CHANNEL_ATS = "ats_jobs"
CHANNEL_LINKEDIN = "linkedin_jobs"
CHANNEL_REED = "reed_jobs"
CHANNEL_CONTRACTS = "contracts_finder"
CHANNEL_BROWSE = "agent_browse"
CHANNEL_WEB_SEARCH = "web_search"
CHANNEL_WEB_NEWS = "web_news"
CHANNEL_UNKNOWN = "unknown"
_QUERY_PREFIX_CHANNELS = (
    ("ats:", CHANNEL_ATS),
    ("linkedin:", CHANNEL_LINKEDIN),
    ("reed:", CHANNEL_REED),
    ("contracts_finder:", CHANNEL_CONTRACTS),
    ("browse:", CHANNEL_BROWSE),
)


@dataclass(frozen=True)
class Origin:
    channel: str
    query: str  # the search or source that found it (a web query, a board, a browsed page)


def candidate_origin(cand: Candidate) -> Origin:
    """The channel and query of the first hit, the one that put this company on the list."""
    if cand.seeded:
        return Origin(CHANNEL_SEED, cand.name_hint or "")
    if not cand.seed_hits:
        return Origin(CHANNEL_UNKNOWN, "")
    hit = cand.seed_hits[0]
    for prefix, channel in _QUERY_PREFIX_CHANNELS:
        if hit.query.startswith(prefix):
            return Origin(channel, hit.query)
    return Origin(CHANNEL_WEB_NEWS if hit.kind == "news" else CHANNEL_WEB_SEARCH, hit.query)


# ---- LLM schemas -----------------------------------------------------------------------------


class PlannedQuery(BaseModel):
    signal_type: str
    kind: Literal["search", "news"]
    query: str
    rationale: str


class QueryPlanOut(BaseModel):
    queries: list[PlannedQuery]


class HitTriage(BaseModel):
    hit_index: int
    company_name: str | None = Field(
        description="Company the result is ABOUT, copied exactly as written; null if none"
    )
    entity_type: Literal[
        "target_company",
        "recruiter",
        "job_board",
        "publisher",
        "directory",
        "service_provider",
        "marketplace_or_retailer",
        "other",
    ]
    link_is_company_own_site: bool
    icp_fit: int = Field(description="0-10, judged only from the title/snippet against the ICP")
    signal_type: str | None = None
    signal_summary: str | None = None
    category: str | None = Field(
        default=None,
        description="2-4 word description of what the company is, e.g. 'functional coffee brand'",
    )


class TriageOut(BaseModel):
    items: list[HitTriage]


# ---- helpers ---------------------------------------------------------------------------------


@dataclass
class QueryStat:
    query: str
    kind: str
    signal_type: str
    hits: int = 0
    targets: int = 0
    rejected_types: dict = field(default_factory=dict)


# Words that describe what a company is rather than which company it is. Picking one as the name to look
# for matched a publisher's article: for "Brave Bison Commerce" the longest word was "commerce", and a news
# headline "PJH Appoints Brave Bison Commerce ..." on roastbrief.us passed as the agency's site (job 29).
GENERIC_NAME_WORDS = frozenset(
    {
        "commerce",
        "ecommerce",
        "digital",
        "agency",
        "agencies",
        "media",
        "marketing",
        "studio",
        "studios",
        "solutions",
        "creative",
        "design",
        "designs",
        "web",
        "software",
        "technologies",
        "technology",
        "tech",
        "consulting",
        "consultancy",
        "consultants",
        "services",
        "service",
        "global",
        "international",
        "partners",
        "interactive",
        "labs",
        "systems",
        "holdings",
        "enterprises",
        "industries",
        "network",
        "networks",
        "online",
        "development",
        "developers",
        "communications",
        "advertising",
        "production",
        "productions",
        "ventures",
        "innovation",
        "innovations",
        "works",
        "collective",
        "worldwide",
    }
)


def _distinctive_token(name: str) -> str | None:
    """The word that identifies this company: the longest one that is not a generic business word. None when
    the name is only generic words ("Digital Agency"): no lookup is safer than a wrong one."""
    stop = {
        "the",
        "and",
        "ltd",
        "limited",
        "plc",
        "llp",
        "group",
        "uk",
        "co",
        "company",
        "of",
        "brand",
        "brands",
    }
    tokens = [
        t
        for t in re.findall(r"[a-z0-9]+", name.lower())
        if t not in stop and t not in GENERIC_NAME_WORDS and len(t) >= 3
    ]
    return max(tokens, key=len) if tokens else None


def _is_site_page(link: str) -> bool:
    """A company's own page (home, /about, /en), not an article: at most one short path segment. An
    article URL carries its headline as a long slug."""
    segments = [seg for seg in urlparse(link).path.split("/") if seg]
    return len(segments) == 0 or (
        len(segments) == 1 and len(segments[0]) <= 30 and segments[0].count("-") <= 3
    )


def _domain_names(link: str, company_name: str) -> bool:
    domain = normalize_domain(link)
    token = _distinctive_token(company_name)
    return bool(domain and token and not is_third_party(domain) and token in domain.replace("-", ""))


async def resolve_domain_options(
    serper: SerperClient, name: str, gl: str, category: str | None = None, limit: int = 3
) -> list[str]:
    """Possible official websites for a company name, best first: non-third-party results whose domain or
    title carries the name's most distinctive token. The category ("coffee brand") keeps common-word names
    such as 'Reformed' or 'TRIP' from resolving to unrelated organisations."""
    token = _distinctive_token(name)
    if not token:
        return []
    query = f'"{name}" {category} official website' if category else f'"{name}" official website'
    named = [
        h
        for h in await serper.query(query, gl=gl, num=10)
        if (d := normalize_domain(h.link))
        and not is_third_party(d)
        and (
            token in d.replace("-", "")
            # A title-only match must be a page of the site itself, not a listing page or an article about
            # the company on someone else's site (a publisher's headline names the company too).
            or (token in h.title.lower() and not LISTING_WORDS.search(h.title) and _is_site_page(h.link))
        )
    ]
    # Prefer results that also describe the right kind of business ('TRIP' the drinks brand, not trip.com).
    words = _category_words(category)
    on_topic = [h for h in named if any(w in f"{h.title} {h.snippet}".lower() for w in words)]
    ordered = on_topic + [h for h in named if h not in on_topic]
    return list(dict.fromkeys(normalize_domain(h.link) for h in ordered))[:limit]


async def resolve_domain(serper: SerperClient, name: str, gl: str, category: str | None = None) -> str | None:
    options = await resolve_domain_options(serper, name, gl, category, limit=1)
    return options[0] if options else None


def _category_words(category: str | None) -> list[str]:
    generic = {
        "brand",
        "brands",
        "company",
        "business",
        "online",
        "retailer",
        "shop",
        "store",
        "maker",
        "startup",
    }
    return [
        w[:-1] if w.endswith("s") else w
        for w in re.findall(r"[a-z]{4,}", (category or "").lower())
        if w not in generic
    ]


# An ICP made of service businesses: their own pages read like "a provider of services", so a classifier
# can label the very companies we want as service providers (job 24: agencies wanted, agencies rejected).
_SERVICE_ICP = re.compile(
    r"\b(agenc(?:y|ies)|studios?|consultanc(?:y|ies)|consultants?|service firms?|"
    r"professional services|freelancers?|developers?|integrators?|resellers?)\b",
    re.I,
)


def icp_is_service_businesses(spec: JobSpec) -> bool:
    icp = spec.icp or {}
    text = " ".join([spec.ask, " ".join(icp.get("industries") or []), str(icp.get("business_model") or "")])
    return bool(_SERVICE_ICP.search(text))


def is_target(t: "HitTriage", spec: JobSpec) -> bool:
    """A result about a company the client sells to. When the ICP itself is service businesses, a well-fitting
    'service_provider' is one of them, not a competitor to skip."""
    if t.entity_type == "target_company":
        return True
    return (
        t.entity_type == "service_provider"
        and icp_is_service_businesses(spec)
        and t.icp_fit >= get_settings().triage_min_fit + 2
    )


def _spec_context(spec: JobSpec) -> str:
    signals = "\n".join(f"- {s.type}: {s.description}" for s in spec.playbook.signals)
    return (
        f"Ask: {spec.ask}\nCountry: {COUNTRIES.get(spec.countries[0], spec.countries[0])}\nICP: {spec.icp}\n"
        f"Signals:\n{signals}\n"
        f"Negative signals: {spec.playbook.negative}"
    )


async def plan_queries(spec: JobSpec, llm: LLMProvider, stats: list[QueryStat], n: int) -> list[PlannedQuery]:
    history = (
        "\n".join(
            f'- [{s.kind}] "{s.query}": {s.hits} hits, {s.targets} target companies; non-targets: {s.rejected_types}'
            for s in stats
        )
        or "(none yet)"
    )
    # The cheap model: side by side on job 29's ask (2026-11-19, fresh and with job 28's history) it wrote
    # queries as good as the judge's, at a tenth of the price. A weak query only wastes one search: triage
    # and every later check still decide who becomes a lead.
    out = await llm.parse(
        llm.extract_model,
        "You plan Google searches that surface SPECIFIC companies matching the ICP and currently showing one of the "
        f"buying signals. Write {n} new queries. Prefer queries that return the companies themselves or news about "
        "named companies (trade press, regional business news, funding/expansion/launch announcements, company "
        "careers pages) over job boards, recruiters, listicles or service providers. Use the country's own phrasing "
        "and outlets. Learn from the history: drop patterns that returned few target companies, vary the ones that "
        "worked. Never repeat a previous query. 'news' searches return dated news articles.",
        f"{_spec_context(spec)}\n\nPrevious queries and their yield:\n{history}",
        QueryPlanOut,
        max_output_tokens=4000,
        effort="medium",
    )
    seen = {s.query.lower() for s in stats}
    return [q for q in out.queries if q.query.lower() not in seen][:n]


async def triage(spec: JobSpec, llm: LLMProvider, hits: list[SearchHit]) -> list[HitTriage]:
    if not hits:
        return []
    data = "\n".join(f"[{i}] ({h.kind}) {h.title} — {h.snippet} <{h.link}>" for i, h in enumerate(hits))
    out = await llm.parse(
        llm.extract_model,
        "Classify every search result. For each: which single company it is ABOUT (exact name as written in the "
        "line, or null), what kind of entity that is, whether the link is that company's own website, how well the "
        "company fits the ICP judging only from the line (0-10), and the buying signal if one is visible. A job ad "
        "is about the HIRING company, not the job board or recruiter (if the hiring company is unnamed, use null).\n"
        "Entity types: target_company = the KIND OF COMPANY THE CLIENT SELLS TO (the ICP), whatever that company "
        "itself does: if the ICP is agencies, an agency is a target_company. service_provider = a company selling "
        "the SAME thing as the client (a competitor of the client), or a vendor writing about the topic, that is "
        "not in the ICP. publisher, directory, job_board, recruiter, marketplace_or_retailer = sites about other "
        "companies.",
        f"{_spec_context(spec)}\n\nResults:\n{data}",
        TriageOut,
        max_output_tokens=8000,
    )
    valid = []
    for t in out.items:
        if not (0 <= t.hit_index < len(hits)):
            continue
        hit = hits[t.hit_index]
        # The company name must literally appear in the result (no invented companies), or be corroborated by
        # the result's own domain: an RFP PDF on brightwave.co.uk is Brightwave's even if its search snippet
        # never repeats the name. A third-party host corroborates nothing.
        named_in_result = t.company_name and t.company_name.lower() in f"{hit.title} {hit.snippet}".lower()
        if t.company_name and not named_in_result and not _domain_names(hit.link, t.company_name):
            t.company_name = None
        valid.append(t)
    return valid


async def icp_gate(spec: JobSpec, llm: LLMProvider, cands: list[Candidate]) -> list[Candidate]:
    """Source rows pass the same ICP triage as search results before any research is spent on them. A job
    board knows who is hiring, not whether that company is the kind the client sells to: job 18 asked for
    digital agencies and the boards returned fintech and SaaS companies, each researched and scored 0."""
    if not cands:
        return cands
    hits = [c.seed_hits[0] for c in cands if c.seed_hits]
    with_hits = [c for c in cands if c.seed_hits]
    try:
        verdicts = await triage(spec, llm, hits)
    except LLMRefusalError as exc:  # the gate is a saving, not a check anything relies on: keep them all
        logger.warning("ICP gate skipped (%s); researching %d source candidates ungated", exc, len(cands))
        return cands
    keep_idx = {
        t.hit_index for t in verdicts if is_target(t, spec) and t.icp_fit >= get_settings().triage_min_fit
    }
    kept = [c for i, c in enumerate(with_hits) if i in keep_idx]
    if dropped := len(with_hits) - len(kept):
        logger.info("ICP gate dropped %d of %d source candidates", dropped, len(with_hits))
    return kept + [c for c in cands if not c.seed_hits]


# ---- main loop -------------------------------------------------------------------------------

RESOLVE_CONCURRENCY = 6
# Searches held back from query rounds for finding each named target's own website. Without this the
# rounds could spend the whole budget, and every company triage named would then be dropped unresolved.
RESOLVE_RESERVE = 10


async def discover(
    spec: JobSpec,
    serper: SerperClient,
    llm: LLMProvider,
    exclude: set[str],
    limit: int,
    stats_out: list | None = None,
    today: date | None = None,
    sources=None,
    board_checker=None,
) -> list[Candidate]:
    """Candidates with a resolved own domain, best first. `board_checker(domain, platform, slug)` proves a
    job-board candidate's website (see _resolved_domain); without one, names resolve as before. If anything stops discovery part-way, the
    exception carries `.candidates` (everything resolved so far) so the run keeps them."""
    by_domain: dict[str, Candidate] = {}
    stats: list[QueryStat] = []
    try:
        return await _discover(
            spec, serper, llm, exclude, limit, today or date.today(), sources, by_domain, stats, board_checker
        )
    except Exception as exc:
        # Whatever stopped us (cost cap, a provider outage), the companies already resolved are good.
        exc.candidates = list(by_domain.values())
        raise
    finally:
        if stats_out is not None:
            stats_out.extend(vars(s) for s in stats)


async def _discover(
    spec: JobSpec,
    serper: SerperClient,
    llm: LLMProvider,
    exclude: set[str],
    limit: int,
    today: date,
    sources,
    by_domain: dict[str, Candidate],
    stats: list[QueryStat],
    board_checker=None,
) -> list[Candidate]:
    settings = get_settings()
    gl = GL.get(spec.countries[0], "uk")
    tbs = search_window(spec.freshness_days)  # coarse; the exact window is enforced on trusted dates
    budget = spec.max_search_queries
    reserve = min(RESOLVE_RESERVE, max(1, limit), budget // 3)
    by_name: dict[str, Candidate] = {}
    for seed in spec.seeds:  # researched first; still subject to every downstream check
        domain = normalize_domain(seed.domain)
        if domain and domain not in exclude and domain not in by_domain:
            hits = [SearchHit("", url, "", None, "news", "seed") for url in seed.source_urls]
            by_domain[domain] = Candidate(
                domain=domain,
                name_hint=seed.name,
                prior=1.0,
                triage_note=seed.note,
                category=seed.category,
                seed_hits=hits,
                seeded=True,
            )
            by_name[seed.name.lower()] = by_domain[domain]
    if spec.seeds_only or len(by_domain) >= limit:
        return list(by_domain.values()) if spec.seeds_only else list(by_domain.values())[:limit]
    if spec.sources:  # structured datasets first: rows that are already candidate buyers
        found, used = await collect_sources(spec, serper, sources, gl, today, budget)
        budget -= used
        found = await icp_gate(spec, llm, found)
        for cand in found:
            key = cand.name_hint.lower()
            if known := by_name.get(key):
                known.seed_hits += cand.seed_hits
                known.registry_id = known.registry_id or cand.registry_id
                known.prior = max(known.prior, cand.prior)
            else:
                by_name[key] = cand
    queue: list[PlannedQuery] = [
        PlannedQuery(signal_type=s.type, kind=s.kind, query=q, rationale="playbook")
        for s in spec.playbook.signals
        for q in s.queries
    ]

    for _round in range(0 if spec.sources_only else settings.discovery_rounds):
        if budget <= reserve:
            break
        if not queue:
            queue = await plan_queries(
                spec, llm, stats, n=min(settings.discovery_queries_per_round, budget - reserve)
            )
        batch, queue = queue[: max(0, budget - reserve)], []
        if not batch:
            break
        budget -= len(batch)
        results = await asyncio.gather(
            *(serper.query(q.query, kind=q.kind, gl=gl, num=10, tbs=tbs) for q in batch)
        )
        round_stats = [
            QueryStat(q.query, q.kind, q.signal_type, hits=len(r))
            for q, r in zip(batch, results, strict=True)
        ]
        stats.extend(round_stats)
        flat = [(i, h) for i, r in enumerate(results) for h in r]
        triaged = await triage(spec, llm, [h for _, h in flat])

        for t in triaged:
            qi, hit = flat[t.hit_index]
            st = round_stats[qi]
            if not is_target(t, spec) or not t.company_name:
                # A target whose name is not literally in the result is not trusted (no invented names).
                why = t.entity_type if t.entity_type != "target_company" else "unnamed_target"
                st.rejected_types[why] = st.rejected_types.get(why, 0) + 1
                continue
            if t.icp_fit < settings.triage_min_fit:
                st.rejected_types["low_fit"] = st.rejected_types.get("low_fit", 0) + 1
                continue
            st.targets += 1
            key = t.company_name.lower()
            cand = by_name.get(key)
            if cand is None:
                domain = normalize_domain(hit.link)
                own = (
                    domain
                    if (
                        t.link_is_company_own_site
                        and domain
                        and not is_third_party(domain)
                        and hit.kind != "news"
                    )
                    else None
                )
                cand = Candidate(domain=own or "", name_hint=t.company_name)
                by_name[key] = cand
            cand.seed_hits.append(hit)
            cand.prior = max(cand.prior, t.icp_fit / 10)
            cand.category = cand.category or t.category
            if t.signal_summary:
                cand.triage_note = t.signal_summary

        strong = [c for c in by_name.values() if c.prior >= 0.6]
        if len(strong) >= limit or budget <= reserve:
            break

    # Resolve official domains, strongest first, within the remaining query budget. Lookups run in small
    # parallel batches (sized to what is still needed), then are added in rank order, so the result is the
    # same as resolving one at a time, just without waiting on each search in turn.
    pending = sorted(by_name.values(), key=lambda c: -c.prior)
    while pending and len(by_domain) < limit:
        batch: list[Candidate] = []
        while pending and len(batch) < min(RESOLVE_CONCURRENCY, max(1, limit - len(by_domain))):
            cand = pending.pop(0)
            if not cand.domain:
                if budget <= 0:
                    continue
                budget -= 1
            batch.append(cand)
        if not batch:
            break
        resolved = await asyncio.gather(*(_resolved_domain(serper, c, gl, board_checker) for c in batch))
        for cand, domain in zip(batch, resolved, strict=True):
            cand.domain = domain
            if len(by_domain) < limit and domain and domain not in exclude and domain not in by_domain:
                by_domain[domain] = cand

    async def real_links(cand: Candidate) -> None:  # real URLs for the evidence we will actually use
        links = await asyncio.gather(*(serper.resolve_redirect(h.link) for h in cand.seed_hits))
        cand.seed_hits = [replace(h, link=link) for h, link in zip(cand.seed_hits, links, strict=True)]

    await asyncio.gather(*(real_links(c) for c in by_domain.values()))
    return list(by_domain.values())


def board_ref(cand: Candidate) -> tuple[str, str] | None:
    """(platform, slug) of the job board a job-board candidate came from, else None."""
    for hit in cand.seed_hits:
        if hit.kind == "ats" and hit.query.startswith("ats:"):
            _, platform, slug = hit.query.split(":", 2)
            return platform, slug
    return None


async def _resolved_domain(serper: SerperClient, cand: Candidate, gl: str, board_checker=None) -> str:
    """The candidate's website. For a job-board candidate with a checker, the first option whose site links
    to that very board: a company's own site links to its own jobs ('Sardine' resolved to an investor's page
    about Sardine, which does not). A site that cannot be read is accepted as before: no proof either way."""
    if cand.domain:
        return cand.domain
    board = board_ref(cand)
    if board is None or board_checker is None:
        return await resolve_domain(serper, cand.name_hint, gl, cand.category) or ""
    for domain in await resolve_domain_options(serper, cand.name_hint, gl, cand.category):
        if await board_checker(domain, *board) is not False:  # True, or None (could not read the site)
            return domain
    logger.info("no website for %s links to its %s board; dropped", cand.name_hint, board[0])
    return ""
