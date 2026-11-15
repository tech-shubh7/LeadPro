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

from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.normalize import normalize_domain
from app.pipeline.jobspec import JobSpec
from app.pipeline.sources import collect_sources
from app.providers.llm.openai_provider import OpenAIProvider
from app.providers.search.serper import SearchHit, SerperClient

# Domains that host content about *other* companies (news, job boards, directories, social).
THIRD_PARTY_DOMAINS = frozenset({
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "tiktok.com",
    "indeed.com", "indeed.co.uk", "reed.co.uk", "totaljobs.com", "glassdoor.com", "glassdoor.co.uk",
    "cv-library.co.uk", "monster.co.uk", "jobsite.co.uk", "adzuna.co.uk", "ziprecruiter.co.uk", "jobleads.com",
    "simplyhired.co.uk", "simplyhired.com", "dealroom.co", "dayforcehcm.com", "workable.com", "greenhouse.io",
    "lever.co", "bamboohr.com", "teamtailor.com", "recruitee.com", "personio.de", "breezy.hr", "smartrecruiters.com",
    "bbc.co.uk", "theguardian.com", "telegraph.co.uk", "ft.com", "thetimes.co.uk", "thetimes.com", "independent.co.uk",
    "cityam.com", "insidermedia.com", "businesslive.co.uk", "prolificnorth.co.uk", "uktech.news", "reuters.com",
    "bloomberg.com", "forbes.com", "businessoffashion.com", "retail-gazette.co.uk", "drapersonline.com",
    "thegrocer.co.uk", "cosmeticsbusiness.com", "globalcosmeticsnews.com", "eu-startups.com", "tech.eu",
    "startups.co.uk", "yahoo.com", "msn.com", "dailymail.co.uk", "standard.co.uk", "finsmes.com",
    "fiercehealthcare.com", "techcrunch.com", "sifted.eu", "crunchbase.com", "pitchbook.com", "tracxn.com",
    "cbinsights.com", "companieshouse.gov.uk", "gov.uk", "endole.co.uk", "companycheck.co.uk", "opencorporates.com",
    "yell.com", "wikipedia.org", "amazon.co.uk", "amazon.com", "ebay.co.uk", "trustpilot.com", "medium.com",
    "reddit.com", "prnewswire.com", "businesswire.com", "globenewswire.com", "google.com", "apple.com",
    "studysmarter.co.uk",
    # job aggregators / career-advice and company-listing sites (never a company's own website)
    "expertini.com", "jobgether.com", "talent.com", "jooble.org", "careerjet.co.uk", "jobrapido.com",
    "neuvoo.co.uk", "tealhq.com", "wfh.team", "flowremote.io", "jobs.workable.com", "huntukvisasponsors.com",
    "justlondonjobs.com", "leicesterjobs.uk", "cwjobs.co.uk", "jobserve.com", "talents.studysmarter.co.uk",
    "digitalagencynetwork.com", "designrush.com", "clutch.co", "goodfirms.co", "selectedfirms.co", "techbehemoths.com",
    "sortlist.com", "getlatka.com", "zoominfo.com", "rocketreach.co", "apollo.io", "lusha.com", "dnb.com",
    "uk.indeed.com", "lbbonline.com", "businesscloud.co.uk",
    "thebusinessdesk.com", "bdaily.co.uk", "finance.yahoo.com",
})
STAFFING_WORDS = ("recruitment", "recruiter", "staffing", "manpower", "placement", "headhunt")
GL = {"GB": "uk", "US": "us", "IN": "in"}


# Brands that host pages about other companies under many TLDs and subdomains (glassdoor.be,
# uk.indeed.com, jobs.ashbyhq.com). Matched on any label of the domain, not the exact host, so a new
# country site cannot pass for a company's own website. Only names that are never a buyer's own brand.
THIRD_PARTY_BRANDS = frozenset({
    "indeed", "glassdoor", "ziprecruiter", "monster", "linkedin", "simplyhired", "adzuna", "jooble", "careerjet",
    "totaljobs", "jobrapido", "neuvoo", "jobgether", "expertini", "ashbyhq", "greenhouse", "smartrecruiters",
    "recruitee", "teamtailor", "personio", "bamboohr", "jobvite", "icims", "myworkdayjobs", "workable",
    "breezy", "remoteok", "weworkremotely", "nodesk", "remotive", "wellfound", "himalayas", "workingnomads",
    "builtin", "otta", "crunchbase", "trustpilot", "wikipedia", "facebook", "instagram", "youtube", "tiktok",
})
# A search title like "Temporal jobs - ZipRecruiter" names the company but the page is a listing about it.
LISTING_WORDS = re.compile(r"\b(jobs?|careers? at|hiring|vacanc(?:y|ies)|salar(?:y|ies)|reviews?|job openings)\b", re.I)


def is_third_party(domain: str) -> bool:
    if domain in THIRD_PARTY_DOMAINS or domain.endswith((".gov.uk", ".ac.uk", ".nhs.uk", ".police.uk")):
        return True
    return any(label in THIRD_PARTY_BRANDS for label in domain.split(".")[:-1])


@dataclass
class Candidate:
    domain: str
    name_hint: str | None = None
    seed_hits: list[SearchHit] = field(default_factory=list)
    prior: float = 0.0  # triage fit estimate 0-1, used to research the most promising first
    triage_note: str = ""
    category: str | None = None  # e.g. "functional coffee brand"; disambiguates common-word names
    registry_id: str | None = None  # company number stated by the discovery source itself (e.g. a contract award notice)


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
    company_name: str | None = Field(description="Company the result is ABOUT, copied exactly as written; null if none")
    entity_type: Literal["target_company", "recruiter", "job_board", "publisher", "directory",
                         "service_provider", "marketplace_or_retailer", "other"]
    link_is_company_own_site: bool
    icp_fit: int = Field(description="0-10, judged only from the title/snippet against the ICP")
    signal_type: str | None = None
    signal_summary: str | None = None
    category: str | None = Field(default=None, description="2-4 word description of what the company is, e.g. 'functional coffee brand'")


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


def _distinctive_token(name: str) -> str | None:
    stop = {"the", "and", "ltd", "limited", "plc", "llp", "group", "uk", "co", "company", "of", "brand", "brands"}
    tokens = [t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in stop and len(t) >= 3]
    return max(tokens, key=len) if tokens else None


async def resolve_domain(serper: SerperClient, name: str, gl: str, category: str | None = None) -> str | None:
    """Official website for a company name: first non-third-party result whose domain or title carries
    the name's most distinctive token. The category ("coffee brand") keeps common-word names such as
    'Reformed' or 'TRIP' from resolving to unrelated organisations."""
    token = _distinctive_token(name)
    if not token:
        return None
    query = f'"{name}" {category} official website' if category else f'"{name}" official website'
    named = [h for h in await serper.query(query, gl=gl, num=10)
             if (d := normalize_domain(h.link)) and not is_third_party(d)
             and (token in d.replace("-", "")
                  # a title-only match must not be a listing page about the company
                  or (token in h.title.lower() and not LISTING_WORDS.search(h.title)))]
    # Prefer a result that also describes the right kind of business ('TRIP' the drinks brand, not trip.com).
    words = _category_words(category)
    on_topic = [h for h in named if any(w in f"{h.title} {h.snippet}".lower() for w in words)]
    best = (on_topic or named or [None])[0]
    return normalize_domain(best.link) if best else None


def _category_words(category: str | None) -> list[str]:
    generic = {"brand", "brands", "company", "business", "online", "retailer", "shop", "store", "maker", "startup"}
    return [w[:-1] if w.endswith("s") else w for w in re.findall(r"[a-z]{4,}", (category or "").lower())
            if w not in generic]


def _spec_context(spec: JobSpec) -> str:
    signals = "\n".join(f"- {s.type}: {s.description}" for s in spec.playbook.signals)
    return (f"Ask: {spec.ask}\nCountry: {spec.countries[0]}\nICP: {spec.icp}\nSignals:\n{signals}\n"
            f"Negative signals: {spec.playbook.negative}")


async def plan_queries(spec: JobSpec, llm: OpenAIProvider, stats: list[QueryStat], n: int) -> list[PlannedQuery]:
    history = "\n".join(
        f'- [{s.kind}] "{s.query}": {s.hits} hits, {s.targets} target companies; non-targets: {s.rejected_types}'
        for s in stats) or "(none yet)"
    out = await llm.parse(
        llm.judge_model,
        "You plan Google searches that surface SPECIFIC companies matching the ICP and currently showing one of the "
        f"buying signals. Write {n} new queries. Prefer queries that return the companies themselves or news about "
        "named companies (trade press, regional business news, funding/expansion/launch announcements, company "
        "careers pages) over job boards, recruiters, listicles or service providers. Use the country's own phrasing "
        "and outlets. Learn from the history: drop patterns that returned few target companies, vary the ones that "
        "worked. Never repeat a previous query. 'news' searches return dated news articles.",
        f"{_spec_context(spec)}\n\nPrevious queries and their yield:\n{history}",
        QueryPlanOut,
        effort="medium",
    )
    seen = {s.query.lower() for s in stats}
    return [q for q in out.queries if q.query.lower() not in seen][:n]


async def triage(spec: JobSpec, llm: OpenAIProvider, hits: list[SearchHit]) -> list[HitTriage]:
    if not hits:
        return []
    data = "\n".join(f"[{i}] ({h.kind}) {h.title} — {h.snippet} <{h.link}>" for i, h in enumerate(hits))
    out = await llm.parse(
        llm.extract_model,
        "Classify every search result. For each: which single company it is ABOUT (exact name as written in the "
        "line, or null), what kind of entity that is, whether the link is that company's own website, how well the "
        "company fits the ICP judging only from the line (0-10), and the buying signal if one is visible. A job ad "
        "is about the HIRING company, not the job board or recruiter (if the hiring company is unnamed, use null).",
        f"{_spec_context(spec)}\n\nResults:\n{data}",
        TriageOut,
    )
    valid = []
    for t in out.items:
        if not (0 <= t.hit_index < len(hits)):
            continue
        hit = hits[t.hit_index]
        # The company name must literally appear in the result (no invented companies).
        if t.company_name and t.company_name.lower() not in f"{hit.title} {hit.snippet}".lower():
            t.company_name = None
        valid.append(t)
    return valid


# ---- main loop -------------------------------------------------------------------------------

RESOLVE_CONCURRENCY = 6
# Searches held back from query rounds for finding each named target's own website. Without this the
# rounds could spend the whole budget, and every company triage named would then be dropped unresolved.
RESOLVE_RESERVE = 10


async def discover(spec: JobSpec, serper: SerperClient, llm: OpenAIProvider, exclude: set[str], limit: int,
                   stats_out: list | None = None, today: date | None = None, sources=None) -> list[Candidate]:
    """Candidates with a resolved own domain, best first. If anything stops discovery part-way, the
    exception carries `.candidates` (everything resolved so far) so the run keeps them."""
    by_domain: dict[str, Candidate] = {}
    stats: list[QueryStat] = []
    try:
        return await _discover(spec, serper, llm, exclude, limit, today or date.today(), sources, by_domain, stats)
    except Exception as exc:
        # Whatever stopped us (cost cap, a provider outage), the companies already resolved are good.
        exc.candidates = list(by_domain.values())
        raise
    finally:
        if stats_out is not None:
            stats_out.extend(vars(s) for s in stats)


async def _discover(spec: JobSpec, serper: SerperClient, llm: OpenAIProvider, exclude: set[str], limit: int,
                    today: date, sources, by_domain: dict[str, Candidate], stats: list[QueryStat]) -> list[Candidate]:
    settings = get_settings()
    gl = GL.get(spec.countries[0], "uk")
    tbs = "qdr:m" if spec.freshness_days <= 31 else "qdr:y"  # coarse; exact window enforced on trusted dates
    budget = spec.max_search_queries
    reserve = min(RESOLVE_RESERVE, max(1, limit), budget // 3)
    by_name: dict[str, Candidate] = {}
    for seed in spec.seeds:  # researched first; still subject to every downstream check
        domain = normalize_domain(seed.domain)
        if domain and domain not in exclude and domain not in by_domain:
            hits = [SearchHit("", url, "", None, "news", "seed") for url in seed.source_urls]
            by_domain[domain] = Candidate(domain=domain, name_hint=seed.name, prior=1.0, triage_note=seed.note,
                                          category=seed.category, seed_hits=hits)
            by_name[seed.name.lower()] = by_domain[domain]
    if spec.seeds_only or len(by_domain) >= limit:
        return list(by_domain.values()) if spec.seeds_only else list(by_domain.values())[:limit]
    if spec.sources:  # structured datasets first: rows that are already candidate buyers
        found, used = await collect_sources(spec, serper, sources, gl, today, budget)
        budget -= used
        for cand in found:
            key = cand.name_hint.lower()
            if known := by_name.get(key):
                known.seed_hits += cand.seed_hits
                known.registry_id = known.registry_id or cand.registry_id
                known.prior = max(known.prior, cand.prior)
            else:
                by_name[key] = cand
    queue: list[PlannedQuery] = [PlannedQuery(signal_type=s.type, kind=s.kind, query=q, rationale="playbook")
                                 for s in spec.playbook.signals for q in s.queries]

    for _round in range(0 if spec.sources_only else settings.discovery_rounds):
        if budget <= reserve:
            break
        if not queue:
            queue = await plan_queries(spec, llm, stats, n=min(settings.discovery_queries_per_round, budget - reserve))
        batch, queue = queue[:max(0, budget - reserve)], []
        if not batch:
            break
        budget -= len(batch)
        results = await asyncio.gather(*(serper.query(q.query, kind=q.kind, gl=gl, num=10, tbs=tbs) for q in batch))
        round_stats = [QueryStat(q.query, q.kind, q.signal_type, hits=len(r)) for q, r in zip(batch, results, strict=True)]
        stats.extend(round_stats)
        flat = [(i, h) for i, r in enumerate(results) for h in r]
        triaged = await triage(spec, llm, [h for _, h in flat])

        for t in triaged:
            qi, hit = flat[t.hit_index]
            st = round_stats[qi]
            if t.entity_type != "target_company" or not t.company_name:
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
                own = domain if (t.link_is_company_own_site and domain and not is_third_party(domain)
                                 and hit.kind != "news") else None
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
        resolved = await asyncio.gather(*(_resolved_domain(serper, c, gl) for c in batch))
        for cand, domain in zip(batch, resolved, strict=True):
            cand.domain = domain
            if len(by_domain) < limit and domain and domain not in exclude and domain not in by_domain:
                by_domain[domain] = cand

    async def real_links(cand: Candidate) -> None:  # real URLs for the evidence we will actually use
        links = await asyncio.gather(*(serper.resolve_redirect(h.link) for h in cand.seed_hits))
        cand.seed_hits = [replace(h, link=link) for h, link in zip(cand.seed_hits, links, strict=True)]

    await asyncio.gather(*(real_links(c) for c in by_domain.values()))
    return list(by_domain.values())


async def _resolved_domain(serper: SerperClient, cand: Candidate, gl: str) -> str:
    if cand.domain:
        return cand.domain
    return await resolve_domain(serper, cand.name_hint, gl, cand.category) or ""
