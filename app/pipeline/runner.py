"""Runs a job end to end: discover -> research -> extract/verify -> score -> contacts -> drafts -> export.

Each successful lead is persisted immediately inside its worker (durable before any later crash), not
batched at the end. An unhandled error anywhere marks the job FAILED with the error recorded, rather
than leaving it stuck in a running status forever.
"""
import asyncio
import logging
import math
import traceback
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from app.config import get_settings
from app.core.cost import CostCapExceeded, CostTracker
from app.core.fetcher import Fetcher
from app.core.normalize import normalize_domain
from app.countries import get_pack
from app.countries.base import REGISTRY_MATCH_NAME, REGISTRY_MATCH_NUMBER
from app.db import session_scope
from app.models.enums import AliveProof, JobStatus, LeadTier
from app.models.tables import Job
from app.pipeline import store
from app.pipeline.contacts import attach_emails, rank_decision_makers
from app.pipeline.discover import GL, STAFFING_WORDS, Candidate, discover
from app.pipeline.drafts import write_draft
from app.pipeline.export import LeadResult, write_outputs
from app.pipeline.extract import extract
from app.pipeline.jobspec import JobSpec
from app.pipeline.research import research
from app.pipeline.score import fresh_signals, score
from app.providers.email.generect import GenerectFinder
from app.providers.llm.manual_provider import ManualLLMProvider
from app.providers.llm.openai_provider import OpenAIProvider
from app.providers.registry.companies_house import CompaniesHouseClient
from app.providers.search.serper import SerperClient

log = logging.getLogger("leadpro")


class Rejected(Exception):
    def __init__(self, reason: str, score=None):
        super().__init__(reason)
        self.score = score


@dataclass
class Deps:
    serper: SerperClient
    llm: OpenAIProvider
    fetcher: Fetcher
    finders: list
    registry: CompaniesHouseClient | None


@dataclass
class RunGuards:
    """Per-run exclusions checked inside each candidate, before any paid step."""
    suppressed_emails: frozenset[str] = frozenset()
    # (registry, number) of companies already delivered to this client or already researched in this run:
    # catches the same registered company reached through a second domain (acme.co.uk vs acme.com).
    claimed_registry: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class RunSummary:
    job_id: int
    status: JobStatus
    qualified: int
    no_contact: int
    icp_only: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    cost_inr: float = 0.0
    outputs: dict = field(default_factory=dict)


def default_deps(cost: CostTracker, countries: list[str]) -> Deps:
    llm = ManualLLMProvider(cost) if get_settings().llm_mode == "manual" else OpenAIProvider(cost)
    return Deps(SerperClient(cost), llm, Fetcher(), [GenerectFinder(cost)],
                CompaniesHouseClient() if "GB" in countries else None)


async def _registry_record(deps: Deps, pack, bundle, company_name: str):
    """Returns (record, how it was matched) or (None, None)."""
    if deps.registry is None or pack.registry_name != "companies_house":
        return None, None
    for number in bundle.company_numbers:  # printed company number: exact, best match
        if record := await deps.registry.profile(number):
            return record, REGISTRY_MATCH_NUMBER
    if record := await deps.registry.match_by_name(company_name):
        return record, REGISTRY_MATCH_NAME
    return None, None


async def process_candidate(c: Candidate, spec: JobSpec, deps: Deps, today: date, lead_id: str,
                            guards: RunGuards | None = None) -> LeadResult:
    settings = get_settings()
    guards = guards or RunGuards()
    pack = get_pack(spec.countries[0])
    bundle = await research(c, deps.fetcher, pack.phone_region, serper=deps.serper,
                            gl=GL.get(pack.code, "uk"), news_queries=settings.company_news_queries)
    if not bundle.website_live and not bundle.website_blocked:
        raise Rejected("website unreachable")
    ext = await extract(bundle, spec, deps.llm, today)
    if not ext.sources_match_target:
        raise Rejected(f"website match is a different organisation than '{c.name_hint}'")
    staffing_wanted = any(w in spec.ask.lower() for w in STAFFING_WORDS)
    if ext.is_staffing and not staffing_wanted:
        raise Rejected("recruitment/staffing firm")
    record, registry_match = await _registry_record(deps, pack, bundle, ext.company_name)
    if record and registry_match == REGISTRY_MATCH_NUMBER:
        key = (record.registry, record.registry_id)
        if key in guards.claimed_registry:  # check-and-claim has no await between: safe under asyncio
            raise Rejected(f"duplicate company: {record.registry} {record.registry_id} already delivered to this "
                           "client or researched in this run under another domain")
        guards.claimed_registry.add(key)
    if record and record.status != "active":
        raise Rejected(f"registry status '{record.status}' ({record.registry} {record.registry_id})")
    proofs = {AliveProof.WEBSITE_LIVE if bundle.website_live else AliveProof.WEBSITE_EXISTS}
    if record and record.status == "active":
        proofs.add(AliveProof.REGISTRY_ACTIVE)
    if fresh_signals(ext, today, spec.freshness_days):
        proofs.add(AliveProof.RECENT_DATED_SIGNAL)
    if AliveProof.RECENT_DATED_SIGNAL not in proofs and not spec.include_icp_only:
        raise Rejected(f"no verified signal dated within {spec.freshness_days} days")
    if not pack.is_alive(proofs):
        raise Rejected(f"not enough proof the business is active ({', '.join(sorted(proofs))})")
    sc = await score(ext, spec, deps.llm, today)
    if AliveProof.RECENT_DATED_SIGNAL in proofs and sc.total >= settings.qualify_score:
        tier = LeadTier.SIGNAL
    elif spec.include_icp_only and sc.fit >= settings.icp_only_min_fit:
        tier = LeadTier.ICP_ONLY  # qualifies on fit alone; delivered on its own, clearly labelled sheet
    elif AliveProof.RECENT_DATED_SIGNAL not in proofs:
        raise Rejected(f"score {sc.total}: no signal dated within {spec.freshness_days} days and ICP fit "
                       f"{sc.fit} below {settings.icp_only_min_fit}: {sc.why}", score=sc)
    else:
        raise Rejected(f"score {sc.total} below {settings.qualify_score}: {sc.why}", score=sc)

    officers, officers_url = [], None
    if record and deps.registry:
        officers = await deps.registry.decision_officers(record.registry_id)
        officers_url = f"https://find-and-update.company-information.service.gov.uk/company/{record.registry_id}/officers"
    people = rank_decision_makers(ext, officers, officers_url, spec.playbook.buyer_titles, settings.max_decision_makers)
    await attach_emails(people, bundle.emails, bundle.domain, deps.finders,
                        known=await asyncio.to_thread(store.known_verified_emails, bundle.domain,
                                                      settings.email_reuse_max_days),
                        suppressed_emails=guards.suppressed_emails)
    decision = pack.outreach_decision(record)
    lead = LeadResult(
        lead_id=lead_id, bundle=bundle, extraction=ext, score=sc,
        registry={"registry_name": record.registry, "registry_id": record.registry_id, "entity_type": record.entity_type,
                  "status": record.status, "legal_name": record.legal_name, "match": registry_match,
                  "incorporated_on": record.incorporated_on.isoformat() if record.incorporated_on else ""} if record else {},
        alive_proofs=sorted(p.value for p in proofs), outreach_allowed=decision.allowed,
        outreach_reason=decision.reason, people=people, tier=tier,
    )
    # Drafts need a genuine dated hook; an ICP-only lead has none, so no email is written (no invented urgency).
    if spec.personalized_email and decision.allowed and tier == LeadTier.SIGNAL:
        for p in lead.verified_people:
            if draft := await write_draft(p, ext, sc, spec, deps.llm, today):
                lead.drafts[(p.first_name, p.last_name)] = draft
    return lead


async def run_job(spec: JobSpec, out_dir: Path, deps: Deps | None = None, today: date | None = None) -> RunSummary:
    settings = get_settings()
    today = today or date.today()
    cost = CostTracker(spec.cost_cap_inr or settings.max_cost_per_job_inr)
    deps = deps or default_deps(cost, spec.countries)
    country = spec.countries[0]

    with session_scope() as s:
        job = Job(client_key=spec.client.key, client_name=spec.client.name, client_website=spec.client.website,
                  ask=spec.ask, target_countries=spec.countries, requested_count=spec.count,
                  freshness_days=spec.freshness_days, personalized_email=spec.personalized_email,
                  icp={"icp": spec.icp, "playbook": spec.playbook.model_dump()}, suppressed_domains=spec.suppress_domains,
                  status=JobStatus.DISCOVERING, cost_cap_inr=cost.cap_inr)
        s.add(job)
        s.flush()
        job_id = job.id
        exclude = (store.delivered_domains(s, spec.client.key) | store.suppressed_domains(s)
                  | {d.lower() for d in spec.suppress_domains})
        guards = RunGuards(store.suppressed_emails(s), store.delivered_registry_keys(s, spec.client.key))
    if spec.client.website and (own := normalize_domain(spec.client.website)):
        exclude.add(own)

    summary = RunSummary(job_id, JobStatus.DONE, 0, 0)
    leads: list[LeadResult] = []
    candidates: list[Candidate] = []
    query_stats: list[dict] = []
    stop = asyncio.Event()

    try:
        try:
            candidates = await discover(spec, deps.serper, deps.llm, exclude,
                                        math.ceil(spec.count * settings.overfetch_factor), stats_out=query_stats)
        except CostCapExceeded as exc:
            summary.status = JobStatus.DONE_WITH_SHORTFALL
            log.warning("cost cap reached during discovery: %s", exc)
        _set_status(job_id, JobStatus.RESEARCHING, {"candidates": len(candidates)})
        sem = asyncio.Semaphore(settings.research_concurrency)

        async def worker(i: int, c: Candidate) -> None:
            async with sem:
                if stop.is_set():
                    return
                try:
                    lead = await process_candidate(c, spec, deps, today, f"L{job_id}-{i:03d}", guards)
                    # Persisted immediately: durable before any later crash, not batched to job end.
                    with session_scope() as s:
                        lead.contact_ids = store.save_lead(s, s.get(Job, job_id), lead, country)
                except Rejected as r:
                    _reject(job_id, c.domain, country, str(r), summary, r.score)
                    return
                except CostCapExceeded as exc:
                    stop.set()
                    summary.status = JobStatus.DONE_WITH_SHORTFALL
                    log.warning("cost cap reached: %s", exc)
                    return
                except Exception as exc:  # one bad site must not kill the job
                    log.exception("candidate %s failed", c.domain)
                    _reject(job_id, c.domain, country, f"error: {type(exc).__name__}: {exc}", summary)
                    return
                leads.append(lead)
                # Signal leads and (opt-in) ICP-only leads both fill `count`; signal leads are delivered first.
                if sum(1 for l in leads if l.verified_people) >= spec.count:
                    stop.set()

        await asyncio.gather(*(worker(i, c) for i, c in enumerate(candidates, 1)))
        _finalize(job_id, spec, leads, candidates, query_stats, cost, summary, None, today, out_dir)
    except Exception:  # anything unexpected (not a per-candidate failure, already handled above): fail loudly, not silently
        error = traceback.format_exc()
        log.exception("job %s failed", job_id)
        summary.status = JobStatus.FAILED
        try:
            # Whatever succeeded individually is already durable (each worker persisted immediately on
            # success); still record and export it, with the job honestly marked failed and why.
            _finalize(job_id, spec, leads, candidates, query_stats, cost, summary, error, today, out_dir)
        except Exception:
            log.exception("job %s: could not record the failure state either", job_id)

    await _close(deps)
    return summary


def _finalize(job_id: int, spec: JobSpec, leads: list[LeadResult], candidates: list[Candidate],
              query_stats: list[dict], cost: CostTracker, summary: RunSummary, error: str | None,
              today: date, out_dir: Path) -> None:
    """Ranks leads, records deliveries, writes the job's final state, and exports. Called once on a
    normal finish and, with `error` set, again from the failure path to salvage whatever was already
    computed and persisted before the crash."""
    by_score = lambda l: -l.score.total
    qualified = sorted((l for l in leads if l.verified_people and l.tier == LeadTier.SIGNAL), key=by_score)[: spec.count]
    icp_only = sorted((l for l in leads if l.verified_people and l.tier == LeadTier.ICP_ONLY),
                      key=by_score)[: max(0, spec.count - len(qualified))]  # only fills the gap
    no_contact = [l for l in leads if not l.verified_people]
    deliver = qualified + icp_only + no_contact
    if summary.status == JobStatus.DONE and len(qualified) + len(icp_only) < spec.count:
        summary.status = JobStatus.DONE_WITH_SHORTFALL
    with session_scope() as s:
        job = s.get(Job, job_id)
        store.record_deliveries(s, job, [(lead, lead.contact_ids) for lead in deliver])
        job.status = summary.status
        job.error = error
        job.cost_inr = round(cost.spent_inr, 2)
        job.cost_lines = [vars(l) for l in cost.lines]
        job.progress = {"candidates": len(candidates), "qualified": len(qualified), "icp_only": len(icp_only),
                        "no_contact": len(no_contact),
                        "rejected": summary.rejected, "discovery_queries": query_stats}
        job.finished_at = datetime.now(UTC)
    summary.outputs = write_outputs(deliver, spec, today, out_dir, job_id)
    summary.qualified, summary.icp_only, summary.no_contact = len(qualified), len(icp_only), len(no_contact)
    summary.cost_inr = round(cost.spent_inr, 2)


async def _close(deps: Deps) -> None:
    await deps.fetcher.aclose()
    for obj in (deps.serper, deps.registry, *deps.finders):
        client = getattr(obj, "client", None)
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()


def _set_status(job_id: int, status: JobStatus, progress: dict) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status, job.progress = status, progress


def _reject(job_id: int, domain: str, country: str, reason: str, summary: RunSummary, score=None) -> None:
    key = reason.split(":")[0].split(" (")[0][:60]
    if key.startswith("score "):
        key = "score below threshold"
    summary.rejected[key] = summary.rejected.get(key, 0) + 1
    with session_scope() as s:
        store.save_rejection(s, s.get(Job, job_id), domain, country, reason, score)
