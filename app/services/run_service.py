"""Runs a job end to end: discover -> research -> extract/verify -> score -> contacts -> drafts -> export.

Each successful lead is persisted immediately inside its worker (durable before any later crash), not
batched at the end. An unhandled error anywhere marks the job FAILED with the error recorded, rather
than leaving it stuck in a running status forever.
"""

import asyncio
import math
import re
import traceback
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from app.compliance import get_pack
from app.compliance.country_pack import (
    REGISTRY_MATCH_LEGAL_NAME,
    REGISTRY_MATCH_NAME,
    REGISTRY_MATCH_NUMBER,
    REGISTRY_MATCH_OFFICER,
    REGISTRY_MATCH_SOURCE,
)
from app.config.constants import STAFFING_WORDS
from app.config.database import session_scope
from app.config.settings import get_settings
from app.exceptions.custom_exceptions import (
    CandidateRejectedError,
    CostCapExceededError,
    LLMRefusalError,
    SuppressedError,
)
from app.models import Job
from app.models.enums import AliveProof, JobStatus, LeadStatus, LeadTier
from app.providers.agents.tinyfish_agent_client import TinyFishAgentClient
from app.providers.email.email_finder_factory import create_email_finders
from app.providers.llm.llm_factory import create_llm
from app.providers.llm.llm_provider import LLMProvider
from app.providers.registry.companies_house_client import CompaniesHouseClient, normalize_company_name
from app.providers.search.search_client import SearchClient
from app.providers.search.search_client_factory import create_search_client
from app.providers.web.page_fetcher import Fetcher
from app.repositories import lead_repository as store
from app.schemas.job_spec_schema import JobSpec
from app.services.contact_service import attach_emails, rank_decision_makers
from app.services.discovery_service import GL, Candidate, discover
from app.services.draft_service import write_draft
from app.services.export_service import LeadResult, rank_key, write_outputs
from app.services.extraction_service import extract
from app.services.research_service import research
from app.services.revalidation_service import revalidate
from app.services.scoring_service import fresh_signals, score
from app.services.signal_intelligence_service import (
    INTENT_LABELS,
    TIER_WEAK,
    WEAK_INTENT_CAP,
    assess_signals,
    can_qualify,
    combine,
)
from app.services.source_service import board_checker_for
from app.utils.cost_tracker import CostTracker, UsageLine
from app.utils.logger import get_logger
from app.utils.normalize import normalize_domain

logger = get_logger(__name__)


@dataclass
class Deps:
    serper: SearchClient  # web search (Serper, with TinyFish as fallback)
    llm: LLMProvider
    fetcher: Fetcher
    finders: list
    registry: CompaniesHouseClient | None
    agent: TinyFishAgentClient | None = None  # browser agent for agent_browse sources; None without a key


@dataclass
class RunGuards:
    """Per-run exclusions checked inside each candidate, before any paid step."""

    suppressed_emails: frozenset[str] = frozenset()
    # (registry, number) of companies already delivered to this client: never researched again under any domain.
    delivered_registry: set[tuple[str, str]] = field(default_factory=set)
    # (registry, number) -> outcome of the domain researching it in this run (True = it became a lead). Catches
    # the same registered company reached through a second domain (acme.co.uk vs acme.com).
    claims: dict[tuple[str, str], asyncio.Future] = field(default_factory=dict)


async def claim_registry(guards: RunGuards, key: tuple[str, str]) -> asyncio.Future:
    """One domain per registered company is researched at a time. A second domain waits for the first one's
    outcome: if it became a lead, the second is a duplicate; if it was rejected (say, no dated signal on that
    domain), the second takes over the claim instead of the company being lost. Returns the future the caller
    must resolve (True = lead, False = rejected)."""
    while True:
        if key in guards.delivered_registry:
            raise CandidateRejectedError(
                f"duplicate company: {key[0]} {key[1]} already delivered to this client"
            )
        holder = guards.claims.get(key)
        if holder is None or (holder.done() and not holder.result()):
            mine = asyncio.get_running_loop().create_future()
            guards.claims[key] = mine  # no await since the check: safe under asyncio
            return mine
        if holder.done():
            raise CandidateRejectedError(
                f"duplicate company: {key[0]} {key[1]} already researched in this run under another domain"
            )
        await asyncio.shield(holder)


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
    stopped_by_cap: bool = False  # the budget ended the run: it can be continued with a bigger one


def default_deps(cost: CostTracker, spec: JobSpec, llm_mode: str | None = None) -> Deps:
    return Deps(
        create_search_client(cost),
        create_llm(cost, llm_mode),
        Fetcher(),
        # A signals-only run never looks anyone up: no paid email finder is even constructed.
        create_email_finders(cost) if spec.wants_contacts else [],
        CompaniesHouseClient() if "GB" in spec.countries else None,
        TinyFishAgentClient(cost) if get_settings().tinyfish_api_key else None,
    )


def _brand_words(company_name: str, domain: str) -> set[str]:
    generic = {"group", "company", "limited", "ltd", "holdings", "services", "solutions", "and", "the", "uk"}
    words = set(normalize_company_name(company_name).split()) | {
        re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    }
    return {w for w in words if len(w) >= 4 and w not in generic}


async def _registry_record(
    deps: Deps, pack, bundle, company_name: str, people=(), source_number: str | None = None
):
    """Returns (record, how it was matched) or (None, None)."""
    if deps.registry is None or pack.registry_name != "companies_house":
        return None, None
    if source_number and (
        record := await deps.registry.profile(source_number)
    ):  # stated by the award notice itself
        return record, REGISTRY_MATCH_SOURCE
    for number in bundle.company_numbers:  # printed company number: exact, best match
        if record := await deps.registry.profile(number):
            return record, REGISTRY_MATCH_NUMBER
    for name in bundle.legal_names:  # registered name printed on its own pages; brand names often differ
        if record := await deps.registry.match_by_name(name):
            return record, REGISTRY_MATCH_LEGAL_NAME
    if record := await deps.registry.match_by_name(company_name):
        return record, REGISTRY_MATCH_NAME
    words = _brand_words(company_name, bundle.domain)
    for person in list(people)[:3]:  # people are quote-verified as working at the target
        if record := await deps.registry.match_by_officer(person.first_name, person.last_name, words):
            return record, REGISTRY_MATCH_OFFICER
    return None, None


async def process_candidate(
    c: Candidate, spec: JobSpec, deps: Deps, today: date, lead_id: str, guards: RunGuards | None = None
) -> LeadResult:
    settings = get_settings()
    guards = guards or RunGuards()
    pack = get_pack(spec.countries[0])
    bundle = await research(
        c,
        deps.fetcher,
        pack.phone_region,
        serper=deps.serper,
        gl=GL.get(pack.code, "uk"),
        news_queries=settings.company_news_queries,
    )
    if not bundle.website_live and not bundle.website_blocked:
        raise CandidateRejectedError("website unreachable")
    ext = await extract(bundle, spec, deps.llm, today)
    if not ext.sources_match_target:
        raise CandidateRejectedError(f"website match is a different organisation than '{c.name_hint}'")
    staffing_wanted = any(w in spec.ask.lower() for w in STAFFING_WORDS)
    if ext.is_staffing and not staffing_wanted:
        raise CandidateRejectedError("recruitment/staffing firm")
    if c.registry_id and bundle.company_numbers and c.registry_id not in bundle.company_numbers:
        # The website prints a different registered number than the award notice's supplier: wrong site.
        raise CandidateRejectedError(
            f"website prints company number {', '.join(bundle.company_numbers)}, "
            f"not the notice supplier's {c.registry_id}"
        )
    record, registry_match = await _registry_record(
        deps, pack, bundle, ext.company_name, ext.people, c.registry_id
    )
    claim = None
    if record and registry_match in (REGISTRY_MATCH_NUMBER, REGISTRY_MATCH_SOURCE):
        claim = await claim_registry(guards, (record.registry, record.registry_id))
    try:
        lead = await _qualify(
            c, spec, deps, today, lead_id, guards, pack, bundle, ext, record, registry_match
        )
    except BaseException:
        if claim is not None and not claim.done():
            claim.set_result(False)  # a waiting duplicate domain may still qualify the company
        raise
    if claim is not None and not claim.done():
        claim.set_result(True)
    return lead


async def _qualify(
    c: Candidate,
    spec: JobSpec,
    deps: Deps,
    today: date,
    lead_id: str,
    guards: RunGuards,
    pack,
    bundle,
    ext,
    record,
    registry_match,
) -> LeadResult:
    settings = get_settings()
    if record and record.status != "active":
        raise CandidateRejectedError(
            f"registry status '{record.status}' ({record.registry} {record.registry_id})"
        )
    seed_kinds = {hit.link: hit.kind for hit in bundle.seed_hits}
    assessment = await assess_signals(
        ext.signals, spec, ext.company_name, bundle.domain, seed_kinds, deps.llm
    )
    ext.signals, ext.against = assessment.buying, assessment.against
    proofs = {AliveProof.WEBSITE_LIVE if bundle.website_live else AliveProof.WEBSITE_EXISTS}
    if record and record.status == "active":
        proofs.add(AliveProof.REGISTRY_ACTIVE)
    if any(can_qualify(s) for s in fresh_signals(ext, today, spec.freshness_days)):
        proofs.add(AliveProof.RECENT_DATED_SIGNAL)
    if AliveProof.RECENT_DATED_SIGNAL not in proofs and not spec.include_icp_only:
        if ext.against:  # the signs found point the other way: say so, it is the useful reason
            first = ext.against[0]
            raise CandidateRejectedError(
                f"no buying intent: {INTENT_LABELS.get(first.intent, first.intent).lower()} ({first.intent_reason})"
            )
        raise CandidateRejectedError(f"no verified buying sign dated within {spec.freshness_days} days")
    if not pack.is_alive(proofs):
        raise CandidateRejectedError(f"not enough proof the business is active ({', '.join(sorted(proofs))})")
    qualifying = [s for s in fresh_signals(ext, today, spec.freshness_days) if can_qualify(s)]
    stack = combine(qualifying)
    sc = await score(ext, spec, deps.llm, today, stack.summary)
    if stack.tier == TIER_WEAK:  # budget alone never reads as strong intent, whatever the scorer says
        sc.intent = min(sc.intent, WEAK_INTENT_CAP)
    if AliveProof.RECENT_DATED_SIGNAL in proofs and sc.total >= settings.qualify_score:
        tier = LeadTier.SIGNAL
    elif spec.include_icp_only and sc.fit >= settings.icp_only_min_fit:
        tier = LeadTier.ICP_ONLY  # qualifies on fit alone; delivered on its own, clearly labelled sheet
    elif AliveProof.RECENT_DATED_SIGNAL not in proofs:
        raise CandidateRejectedError(
            f"score {sc.total}: no signal dated within {spec.freshness_days} days and ICP fit "
            f"{sc.fit} below {settings.icp_only_min_fit}: {sc.why}",
            score=sc,
        )
    else:
        raise CandidateRejectedError(f"score {sc.total} below {settings.qualify_score}: {sc.why}", score=sc)

    people = await _decision_makers(spec, deps, guards, bundle, ext, record) if spec.wants_contacts else []
    decision = pack.outreach_decision(record)
    lead = LeadResult(
        lead_id=lead_id,
        bundle=bundle,
        extraction=ext,
        score=sc,
        stack=stack,
        registry={
            "registry_name": record.registry,
            "registry_id": record.registry_id,
            "entity_type": record.entity_type,
            "status": record.status,
            "legal_name": record.legal_name,
            "match": registry_match,
            "incorporated_on": record.incorporated_on.isoformat() if record.incorporated_on else "",
        }
        if record
        else {},
        alive_proofs=sorted(p.value for p in proofs),
        outreach_allowed=decision.allowed,
        outreach_reason=decision.reason,
        people=people,
        tier=tier,
        contacts_wanted=spec.wants_contacts,
    )
    # Drafts need a genuine dated hook; an ICP-only lead has none, so no email is written (no invented urgency).
    if spec.personalized_email and decision.allowed and tier == LeadTier.SIGNAL:
        for p in lead.verified_people:
            if draft := await write_draft(p, ext, sc, spec, deps.llm, today):
                lead.drafts[(p.first_name, p.last_name)] = draft
    return lead


async def _decision_makers(spec: JobSpec, deps: Deps, guards: RunGuards, bundle, ext, record) -> list:
    """Ranked decision-makers with verified emails attached (contacts mode only: paid finders, PII)."""
    settings = get_settings()
    officers, officers_url = [], None
    if record and deps.registry:
        officers = await deps.registry.decision_officers(record.registry_id)
        officers_url = f"https://find-and-update.company-information.service.gov.uk/company/{record.registry_id}/officers"
    people = rank_decision_makers(
        ext, officers, officers_url, spec.playbook.buyer_titles, settings.max_decision_makers
    )
    await attach_emails(
        people,
        bundle.emails,
        bundle.domain,
        deps.finders,
        known=await asyncio.to_thread(
            store.known_verified_emails, bundle.domain, settings.email_reuse_max_days
        ),
        suppressed_emails=guards.suppressed_emails,
    )
    return people


async def run_job(
    spec: JobSpec,
    out_dir: Path,
    deps: Deps | None = None,
    today: date | None = None,
    llm_mode: str | None = None,
    job_id: int | None = None,
) -> RunSummary:
    """Runs `spec` end to end. `job_id` adopts a row that already exists (the console creates one at
    intake so the plan can be reviewed before anything is spent); without it a new row is inserted."""
    settings = get_settings()
    today = today or date.today()
    cost = CostTracker(spec.cost_cap_inr or settings.max_cost_per_job_inr)
    deps = deps or default_deps(cost, spec, llm_mode)
    country = spec.countries[0]

    with session_scope() as s:
        fields = {
            "client_key": spec.client.key,
            "client_name": spec.client.name,
            "client_website": spec.client.website,
            "ask": spec.ask,
            "target_countries": spec.countries,
            "requested_count": spec.count,
            "freshness_days": spec.freshness_days,
            "personalized_email": spec.personalized_email,
            "suppressed_domains": spec.suppress_domains,
            "status": JobStatus.DISCOVERING,
            "cost_cap_inr": cost.cap_inr,
            "error": None,
            "finished_at": None,
        }
        job = s.get(Job, job_id) if job_id is not None else None
        if job is None:
            job = Job(icp={"icp": spec.icp, "playbook": spec.playbook.model_dump()}, **fields)
            s.add(job)
        else:
            # Spend already on this job (drafting the plan from the client's site) is part of its cost and
            # counts against the same cap; a fresh tracker would silently drop it from the total.
            if job.cost_lines and not cost.lines:
                cost.lines.extend(UsageLine(**line) for line in job.cost_lines)
            for key, value in fields.items():
                setattr(job, key, value)
            # Keep whatever the intake stored alongside the ICP (the client summary, sender points).
            job.icp = (job.icp or {}) | {"icp": spec.icp, "playbook": spec.playbook.model_dump()}
        s.flush()
        job_id = job.id
        exclude = (
            store.delivered_domains(s, spec.client.key)
            | store.suppressed_domains(s)
            | {d.lower() for d in spec.suppress_domains}
        )
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
            candidates = await discover(
                spec,
                deps.serper,
                deps.llm,
                exclude,
                math.ceil(spec.count * settings.overfetch_factor),
                stats_out=query_stats,
                today=today,
                sources={"agent": deps.agent} if deps.agent else None,
                board_checker=board_checker_for(deps.fetcher),
            )
        except (CostCapExceededError, LLMRefusalError) as exc:
            # The cap, or the model provider having a bad minute. Neither is a reason to throw away the
            # companies discovery already resolved: research them and report the shortfall honestly.
            summary.status = JobStatus.DONE_WITH_SHORTFALL
            summary.stopped_by_cap = summary.stopped_by_cap or isinstance(exc, CostCapExceededError)
            candidates = list(getattr(exc, "candidates", []))
            logger.warning("discovery stopped early (%d candidates kept): %s", len(candidates), exc)
        await asyncio.to_thread(
            _set_status, job_id, JobStatus.RESEARCHING, {"candidates": len(candidates)}, cost
        )
        sem = asyncio.Semaphore(settings.research_concurrency)

        async def worker(i: int, c: Candidate) -> None:
            async with sem:
                if stop.is_set():
                    return
                try:
                    lead = await process_candidate(c, spec, deps, today, f"L{job_id}-{i:03d}", guards)
                    # Persisted immediately: durable before any later crash, not batched to job end.
                    # Sync DB I/O runs off the event loop so other workers' fetches keep moving.
                    lead.contact_ids = await asyncio.to_thread(_persist_lead, job_id, lead, country)
                except CandidateRejectedError as r:
                    await _reject(job_id, c.domain, country, str(r), summary, r.score)
                    return
                except CostCapExceededError as exc:
                    stop.set()
                    summary.status = JobStatus.DONE_WITH_SHORTFALL
                    summary.stopped_by_cap = True
                    logger.warning("cost cap reached: %s", exc)
                    return
                except Exception as exc:  # one bad site must not kill the job
                    logger.exception("candidate %s failed", c.domain)
                    await _reject(job_id, c.domain, country, f"error: {type(exc).__name__}: {exc}", summary)
                    return
                finally:
                    # The console shows spend live; without this it sat at the drafting cost for the whole run.
                    await asyncio.to_thread(_save_cost, job_id, cost)
                leads.append(lead)
                # Stop once enough SIGNAL leads are deliverable (a verified contact, or just the signal in a
                # signals-only run). ICP-only leads only fill the gap left when candidates run out; counting
                # them here would end the search for signal leads early.
                if (
                    sum(1 for lead in leads if lead.deliverable and lead.tier == LeadTier.SIGNAL)
                    >= spec.count
                ):
                    stop.set()

        await asyncio.gather(*(worker(i, c) for i, c in enumerate(candidates, 1)))
        leads = await _revalidate_before_delivery(job_id, leads, deps, today, spec, summary, len(candidates))
        _finalize(job_id, spec, leads, candidates, query_stats, cost, summary, None, today, out_dir)
    except (
        Exception
    ):  # anything unexpected (not a per-candidate failure, already handled above): fail loudly, not silently
        error = traceback.format_exc()
        logger.exception("job %s failed", job_id)
        summary.status = JobStatus.FAILED
        try:
            # Whatever succeeded individually is already durable (each worker persisted immediately on
            # success); still record and export it, with the job honestly marked failed and why.
            _finalize(job_id, spec, leads, candidates, query_stats, cost, summary, error, today, out_dir)
        except Exception:
            logger.exception("job %s: could not record the failure state either", job_id)
    finally:  # clients are closed even on cancellation (Ctrl-C), not only on a normal or failed finish
        await _close(deps)
    return summary


async def _revalidate_before_delivery(
    job_id: int,
    leads: list[LeadResult],
    deps: Deps,
    today: date,
    spec: JobSpec,
    summary: RunSummary,
    candidates: int,
) -> list[LeadResult]:
    """Fresh re-check of every deliverable lead's buying signs; leads whose signs are all gone are held
    back and recorded as dropped on revalidation, with the reason."""
    deliverable = [lead for lead in leads if lead.deliverable]
    if not deliverable:
        return leads
    await asyncio.to_thread(_set_status, job_id, JobStatus.REVALIDATING, {"candidates": candidates})
    dropped = await revalidate(deliverable, deps.fetcher, today, spec.freshness_days)
    if not dropped:
        return leads
    gone = {id(deliverable[i]): reason for i, reason in dropped.items()}
    for lead in deliverable:
        if reason := gone.get(id(lead)):
            await asyncio.to_thread(_mark_dropped, job_id, lead.company_id, reason)
            summary.rejected[reason] = summary.rejected.get(reason, 0) + 1
    return [lead for lead in leads if id(lead) not in gone]


def _mark_dropped(job_id: int, company_id: int | None, reason: str) -> None:
    with session_scope() as s:
        store.mark_not_delivered(s, job_id, [company_id], reason, status=LeadStatus.DROPPED_ON_REVALIDATION)


def _finalize(
    job_id: int,
    spec: JobSpec,
    leads: list[LeadResult],
    candidates: list[Candidate],
    query_stats: list[dict],
    cost: CostTracker,
    summary: RunSummary,
    error: str | None,
    today: date,
    out_dir: Path,
) -> None:
    """Ranks leads, records deliveries, writes the job's final state, and exports. Called once on a
    normal finish and, with `error` set, again from the failure path to salvage whatever was already
    computed and persisted before the crash."""
    with session_scope() as s:
        active_leads = []
        for lead in leads:
            try:
                store.apply_current_suppressions(s, lead)
            except SuppressedError as exc:
                reason = str(exc)
                summary.rejected[reason] = summary.rejected.get(reason, 0) + 1
            else:
                active_leads.append(lead)
        leads = active_leads
        # Every signal lead found is delivered, even past the target: workers already in flight when the
        # target was reached finish their leads, those were paid for, and holding them back left them stored
        # as 'qualified' but never delivered, so the console counted leads the spreadsheet did not have.
        qualified = sorted(
            (lead for lead in leads if lead.deliverable and lead.tier == LeadTier.SIGNAL), key=rank_key
        )
        icp_ranked = sorted(
            (lead for lead in leads if lead.deliverable and lead.tier == LeadTier.ICP_ONLY), key=rank_key
        )
        gap = max(0, spec.count - len(qualified))
        icp_only, icp_unneeded = icp_ranked[:gap], icp_ranked[gap:]
        no_contact = [lead for lead in leads if not lead.deliverable]
        deliver = qualified + icp_only + no_contact
        if summary.status == JobStatus.DONE and len(qualified) + len(icp_only) < spec.count:
            summary.status = JobStatus.DONE_WITH_SHORTFALL
        # Produce the complete deliverable before claiming that any lead was
        # sent. `write_outputs` publishes each file atomically, so a failed
        # workbook/CSV cannot poison the never-repeat delivery registry.
        summary.outputs = write_outputs(deliver, spec, today, out_dir, job_id)
        job = s.get(Job, job_id)
        store.record_deliveries(s, job, [(lead, lead.contact_ids) for lead in deliver])
        if icp_unneeded:  # stored as leads but not delivered: record them as such, so every count agrees
            reason = "ICP-only, not needed: signal leads met the target"
            store.mark_not_delivered(s, job_id, [lead.company_id for lead in icp_unneeded], reason)
            summary.rejected[reason] = summary.rejected.get(reason, 0) + len(icp_unneeded)
        for tier_leads in (qualified, icp_only):  # each sheet ranks from 1, as the export does
            store.set_ranks(s, job_id, [lead.company_id for lead in tier_leads])
        job.status = summary.status
        job.error = error
        job.cost_inr = round(cost.spent_inr, 2)
        job.cost_lines = [vars(lead) for lead in cost.lines]
        job.progress = {
            "candidates": len(candidates),
            "qualified": len(qualified),
            "icp_only": len(icp_only),
            "no_contact": len(no_contact),
            "rejected": summary.rejected,
            "stopped_by_cap": summary.stopped_by_cap,
            "discovery_queries": query_stats,
        }
        job.finished_at = datetime.now(UTC)
    summary.qualified, summary.icp_only, summary.no_contact = len(qualified), len(icp_only), len(no_contact)
    summary.cost_inr = round(cost.spent_inr, 2)


async def _close(deps: Deps) -> None:
    await deps.fetcher.aclose()
    if close := getattr(deps.serper, "aclose", None):
        await close()
    for obj in (deps.registry, deps.agent, *deps.finders):
        client = getattr(obj, "client", None)
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()


def _persist_lead(job_id: int, lead: LeadResult, country: str) -> dict:
    with session_scope() as s:
        try:
            return store.save_lead(s, s.get(Job, job_id), lead, country)
        except SuppressedError as exc:
            raise CandidateRejectedError(str(exc)) from exc


def _set_status(job_id: int, status: JobStatus, progress: dict, cost: CostTracker | None = None) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status, job.progress = status, progress
        if cost is not None:
            job.cost_inr = round(cost.spent_inr, 2)


def _save_cost(job_id: int, cost: CostTracker) -> None:
    try:
        with session_scope() as s:
            if job := s.get(Job, job_id):
                job.cost_inr = round(cost.spent_inr, 2)
    except Exception:  # a display value: never worth failing a lead over
        logger.exception("could not record running cost for job %s", job_id)


def _save_rejection(job_id: int, domain: str, country: str, reason: str, score) -> None:
    with session_scope() as s:
        store.save_rejection(s, s.get(Job, job_id), domain, country, reason, score)


async def _reject(
    job_id: int, domain: str, country: str, reason: str, summary: RunSummary, score=None
) -> None:
    key = reason.split(":")[0].split(" (")[0][:60]
    if key.startswith("score "):
        key = "score below threshold"
    summary.rejected[key] = summary.rejected.get(key, 0) + 1
    if not domain:
        return  # never resolved to a website: nothing to key a company record on
    try:
        await asyncio.to_thread(_save_rejection, job_id, domain, country, reason, score)
    except Exception:  # the audit row is best-effort; failing to write it must not fail the whole job
        logger.exception("could not record rejection of %s", domain)
