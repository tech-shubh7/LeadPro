"""Offline end-to-end run: real pipeline, DB and export; fake search/LLM/registry/finder; mocked websites."""
import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import openpyxl
import respx

from app.core.cost import CostTracker
from app.countries import RegistryRecord
from app.core.fetcher import Fetcher, PageCache
from app.db import session_scope
from app.models.enums import EmailVerdict, JobStatus, LeadStatus, LeadTier
from app.models.tables import Company, Delivery, Job, JobLead
from app.pipeline import contacts, store
from app.pipeline.discover import HitTriage, QueryPlanOut, TriageOut
from app.pipeline.drafts import DraftOut
from app.pipeline.extract import ExtractOut, QuotedFact, QuotedPerson, QuotedSignal
from app.pipeline.jobspec import JobSpec
from app.pipeline.runner import Deps, run_job
from app.pipeline.score import ScoreOut
from app.providers.email import EmailResult
from app.providers.search.serper import SearchHit

TODAY = date(2026, 10, 30)
SITES = {
    "acme.co.uk": ("2026-10-20", "Acme Goods is hiring three warehouse operatives for its Leeds fulfilment hub this winter.",
                   "Jane Roe, Head of Operations, leads our logistics team."),
    "bravo.co.uk": ("2026-10-12", "Bravo Brands announced it raised new funding to expand into national retailers.",
                    "Tom Fox, Operations Director, runs supply chain at Bravo."),
    "charlie.co.uk": ("2024-03-01", "Charlie Ltd is hiring warehouse staff for its new distribution site in Bristol.",
                      "Amy Wu, Head of Operations, oversees fulfilment at Charlie."),
}
COMPANY_NUMBERS = {"acme.co.uk": "01234567", "bravo.co.uk": "07654321", "charlie.co.uk": "09876543"}
FILLER = " We sell quality consumer products online to customers across the United Kingdom." * 6


def _article(domain: str) -> str:
    d, signal, _ = SITES[domain]
    return (f'<html><head><meta property="article:published_time" content="{d}"></head>'
            f"<body><article><h1>News</h1><p>{signal}</p><p>{FILLER}</p></article></body></html>")


def _team(domain: str) -> str:
    return f"<html><body><main><h1>Our team</h1><p>{SITES[domain][2]}</p><p>{FILLER}</p></main></body></html>"


def _home(domain: str) -> str:
    return (f'<html><body><nav><a href="/our-team">Our team</a></nav><main><h1>{domain}</h1><p>{FILLER}</p>'
            f"<footer>Registered in England and Wales No. {COMPANY_NUMBERS[domain]}</footer></main></body></html>")


def news_url(domain: str) -> str:
    y, m, _ = SITES[domain][0].split("-")
    return f"https://{domain}/news/{y}/{m}/update"


class FakeSerper:
    async def resolve_redirect(self, link):
        return link

    async def query(self, q, kind="search", gl="uk", num=10, tbs=None):
        return [SearchHit(f"{d} news", news_url(d), SITES[d][1], None, kind, q) for d in SITES]


class FakeLLM:
    async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
        if schema is QueryPlanOut:
            return QueryPlanOut(queries=[])
        if schema is TriageOut:
            items = []
            for i, line in enumerate(l for l in data.split("\n") if l.startswith("[")):
                d = next((d for d in SITES if d in line), None)
                if d:
                    items.append(HitTriage(hit_index=i, company_name=d.split(".")[0], entity_type="target_company",
                                           link_is_company_own_site=True, icp_fit=8, signal_type="hiring_ops"))
            return TriageOut(items=items)
        if schema is ExtractOut:
            domain = next(d for d in SITES if f"website is {d}" in instructions)
            _, signal, person = SITES[domain]
            first, last = person.split(",")[0].split()
            title = person.split(",")[1].strip()
            return ExtractOut(
                sources_match_target=True,
                company_name=domain.split(".")[0].title(), company_description="Online consumer brand",
                is_recruitment_or_staffing_firm=False,
                facts=[QuotedFact(claim="Sells consumer products online", source_url=f"https://{domain}/",
                                  quote="We sell quality consumer products online to customers")],
                signals=[QuotedSignal(signal_type="hiring_ops", claim=signal, source_url=news_url(domain), quote=signal)],
                people=[QuotedPerson(first_name=first, last_name=last, job_title=title,
                                     source_url=f"https://{domain}/our-team", quote=person),
                        QuotedPerson(first_name="Ghost", last_name="Person", job_title="CEO",  # fabricated: must be dropped
                                     source_url=f"https://{domain}/our-team", quote="Ghost Person, CEO and founder of the firm")],
            )
        if schema is ScoreOut:
            return ScoreOut(fit_score=35, intent_score=99, timing_score=15, why_this_lead="Hiring now [E1]",
                            outreach_angle="Peak season capacity", risks="none known", cited_evidence_ids=["E1", "E7"])
        if schema is DraftOut:
            return DraftOut(subject="Your Leeds hiring", body="Saw you're hiring for the Leeds hub this winter.",
                            cited_evidence_ids=["E1"])
        raise AssertionError(schema)


class FakeRegistry:
    async def profile(self, number):
        return None  # forces the no-registry path: live site + dated signal, outreach blocked

    async def match_by_name(self, name):
        return None

    async def match_by_officer(self, first, last, words):
        return None

    async def decision_officers(self, number):
        return []


class FakeFinder:
    name = "fake"

    async def find(self, first, last, domain):
        if domain == "bravo.co.uk":
            return EmailResult(f"{first.lower()}@{domain}", EmailVerdict.CATCH_ALL, "fake", "valid")
        return EmailResult(f"{first.lower()}.{last.lower()}@{domain}", EmailVerdict.VALID, "fake", "valid")

    async def validate(self, emails):
        return []


def _spec() -> JobSpec:
    return JobSpec.model_validate({
        "client": {"key": "test-client", "name": "Test 3PL", "website": "https://test3pl.co.uk"},
        "ask": "UK e-commerce brands needing fulfilment", "countries": ["GB"], "count": 2,
        "freshness_days": 90, "personalized_email": True, "cost_cap_inr": 50,
        "icp": {"industries": ["e-commerce"]},
        "playbook": {"signals": [{"type": "hiring_ops", "description": "hiring", "queries": ["q1"]}],
                     "buyer_titles": ["Head of Operations", "Operations Director"]},
    })


def _mock_sites():
    for d in SITES:
        respx.get(f"https://{d}/robots.txt").respond(404)
        respx.get(f"https://{d}/").respond(200, html=_home(d))
        respx.get(f"https://{d}/our-team").respond(200, html=_team(d))
        respx.get(news_url(d)).respond(200, html=_article(d))


@respx.mock
def test_full_run_routes_leads_correctly(tmp_path: Path, clean_db, monkeypatch):
    async def mx(domain):
        return True
    monkeypatch.setattr(contacts, "has_mx", mx)
    _mock_sites()

    def deps():
        return Deps(FakeSerper(), FakeLLM(), Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
                    [FakeFinder()], FakeRegistry())

    summary = asyncio.run(run_job(_spec(), tmp_path / "out", deps=deps(), today=TODAY))
    assert summary.qualified == 1 and summary.no_contact == 1
    assert any("dated within 90 days" in k for k in summary.rejected), summary.rejected

    wb = openpyxl.load_workbook(summary.outputs["xlsx"])
    leads = list(wb["Leads"].values)
    header, rows = leads[0], leads[1:]
    row = dict(zip(header, rows[0]))
    assert len(rows) == 1 and row["work_email"] == "jane.roe@acme.co.uk"
    assert row["intent_score"] == 40 and row["total_score"] == 90  # model's 99 clamped; total computed in code
    assert row["signal_1_date"] == "2026-10-20" and row["outreach_allowed"] == "no"  # no registry match in UK
    assert "Ghost" not in str(rows)  # fabricated person dropped by quote check
    assert not row["email_subject"]  # drafts are only written when outreach is allowed
    nc = list(wb["Good leads - no contact"].values)
    nc_row = dict(zip(nc[0], nc[1]))
    assert nc_row["company_domain"] == "bravo.co.uk" and "catch_all" in nc_row["why_no_contact"]

    with session_scope() as s:
        statuses = sorted(l.status for l in s.query(JobLead).all())
        assert LeadStatus.QUALIFIED in statuses and LeadStatus.QUALIFIED_NO_CONTACT in statuses
        assert s.query(Delivery).count() == 2

    # Second run for the same client: already-delivered companies are never returned again.
    second = asyncio.run(run_job(_spec(), tmp_path / "out2", deps=deps(), today=TODAY))
    assert second.qualified == 0 and second.no_contact == 0


def test_cost_cap_is_enforced_by_providers():
    t = CostTracker(cap_inr=1)
    t.record("x", "y", 1, 0.9)
    try:
        t.ensure_budget(0.2)
        raised = False
    except Exception:
        raised = True
    assert raised


class ActiveRegistry(FakeRegistry):
    async def profile(self, number):
        return RegistryRecord("companies_house", number, "ACME GOODS LTD", "ltd", "active", date(2015, 1, 5), datetime.now(UTC))

    async def decision_officers(self, number):
        from app.providers.registry.companies_house import Officer
        return [Officer("Bob", "Smith", "director", None)]


@respx.mock
def test_registry_match_allows_outreach_and_writes_grounded_draft(tmp_path: Path, clean_db, monkeypatch):
    async def mx(domain):
        return True
    monkeypatch.setattr(contacts, "has_mx", mx)
    _mock_sites()
    deps = Deps(FakeSerper(), FakeLLM(), Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
                [FakeFinder()], ActiveRegistry())
    summary = asyncio.run(run_job(_spec(), tmp_path / "out", deps=deps, today=TODAY))
    wb = openpyxl.load_workbook(summary.outputs["xlsx"])
    rows = [dict(zip(list(wb["Leads"].values)[0], r)) for r in list(wb["Leads"].values)[1:]]
    acme = [r for r in rows if r["company_domain"] == "acme.co.uk"]
    assert acme and all(r["outreach_allowed"] == "yes" and r["registry_id"] == "01234567" for r in acme)
    assert {r["full_name"] for r in acme} == {"Jane Roe", "Bob Smith"}  # site person + registry director
    jane = next(r for r in acme if r["full_name"] == "Jane Roe")
    assert jane["email_subject"] == "Your Leeds hiring" and "reply \"no\"" in jane["email_body"]


@respx.mock
def test_blocked_site_is_researched_from_other_sources(tmp_path: Path, clean_db, monkeypatch):
    """A company whose site refuses automated access is kept when news + registry give verified evidence."""
    async def mx(domain):
        return True
    monkeypatch.setattr(contacts, "has_mx", mx)
    for d in SITES:
        respx.get(f"https://{d}/robots.txt").respond(404)
        respx.get(f"https://{d}/our-team").respond(200, html=_team(d))
        respx.get(news_url(d)).respond(200, html=_article(d))
    respx.get("https://acme.co.uk/").respond(429)
    respx.get("https://www.acme.co.uk/robots.txt").respond(404)
    respx.get("https://www.acme.co.uk/").respond(429)
    for d in ("bravo.co.uk", "charlie.co.uk"):
        respx.get(f"https://{d}/").respond(200, html=_home(d))
    deps = Deps(FakeSerper(), FakeLLM(), Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
                [FakeFinder()], ActiveRegistry())
    summary = asyncio.run(run_job(_spec(), tmp_path / "out", deps=deps, today=TODAY))
    wb = openpyxl.load_workbook(summary.outputs["xlsx"])
    rows = []
    for sheet in ("Leads", "Good leads - no contact"):
        values = list(wb[sheet].values)
        rows += [dict(zip(values[0], r)) for r in values[1:]]
    acme = [r for r in rows if r["company_domain"] == "acme.co.uk"]
    assert acme, summary.rejected  # kept (not lost) even though its own site blocked us
    assert "not readable by automated tools" in acme[0]["website_access"]
    assert acme[0]["signal_1_source_url"] == news_url("acme.co.uk")


def test_verified_email_is_reused_without_paying(monkeypatch):
    from app.models.enums import BuyerRole
    from app.pipeline.contacts import DecisionMaker, attach_emails

    class CountingFinder(FakeFinder):
        calls = 0

        async def find(self, first, last, domain):
            CountingFinder.calls += 1
            return await super().find(first, last, domain)

    dm = DecisionMaker("Jane", "Roe", "Head of Ops", BuyerRole.FUNCTIONAL_OWNER, "u", "q")
    asyncio.run(attach_emails([dm], [], "acme.co.uk", [CountingFinder()], known={("jane", "roe"): "jane.roe@acme.co.uk"}))
    assert dm.email == "jane.roe@acme.co.uk" and CountingFinder.calls == 0


def test_wrong_website_match_is_rejected(tmp_path: Path, clean_db, monkeypatch):
    """If extraction says the site belongs to a different organisation, the company is rejected (not scored)."""
    class MismatchLLM(FakeLLM):
        async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
            out = await super().parse(model, instructions, data, schema, effort, max_output_tokens)
            if schema is ExtractOut and "website is charlie.co.uk" in instructions:
                out.sources_match_target = False
            return out

    async def mx(domain):
        return True
    monkeypatch.setattr(contacts, "has_mx", mx)
    with respx.mock:
        _mock_sites()
        deps = Deps(FakeSerper(), MismatchLLM(), Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
                    [FakeFinder()], FakeRegistry())
        summary = asyncio.run(run_job(_spec(), tmp_path / "out", deps=deps, today=TODAY))
    assert any("different organisation" in k for k in summary.rejected), summary.rejected


def test_suppressed_domain_is_excluded_from_discovery(tmp_path: Path, clean_db, monkeypatch):
    """A suppressed domain is never researched: it must not appear as qualified, no-contact, or rejected."""
    async def mx(domain):
        return True
    monkeypatch.setattr(contacts, "has_mx", mx)
    with session_scope() as s:
        store.add_suppression(s, "domain", "acme.co.uk", reason="client asked to never contact this company again")
    with respx.mock:
        _mock_sites()
        deps = Deps(FakeSerper(), FakeLLM(), Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
                    [FakeFinder()], FakeRegistry())
        summary = asyncio.run(run_job(_spec(), tmp_path / "out", deps=deps, today=TODAY))
    assert "acme.co.uk" not in str(summary.rejected)
    with session_scope() as s:
        from app.models.tables import Company
        assert s.query(Company).filter(Company.domain == "acme.co.uk").first() is None  # never researched


def test_discover_crash_marks_job_failed_with_error_recorded(tmp_path: Path, clean_db):
    """An unexpected bug (not a per-candidate Rejected/CostCapExceeded) must not leave the job stuck."""
    class BoomSerper(FakeSerper):
        async def query(self, *a, **k):
            raise RuntimeError("serper exploded unexpectedly")

    deps = Deps(BoomSerper(), FakeLLM(), Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
                [FakeFinder()], FakeRegistry())
    summary = asyncio.run(run_job(_spec(), tmp_path / "out", deps=deps, today=TODAY))
    assert summary.status == JobStatus.FAILED
    assert summary.qualified == 0 and summary.no_contact == 0
    with session_scope() as s:
        job = s.get(Job, summary.job_id)
        assert job.status == JobStatus.FAILED
        assert job.error and "serper exploded unexpectedly" in job.error
        assert job.finished_at is not None  # not stuck in a running state


def test_crash_after_success_still_salvages_already_persisted_leads(tmp_path: Path, clean_db, monkeypatch):
    """Leads already computed and persisted by their workers must survive a later, unrelated crash:
    the job ends FAILED (honest), but the work already done is not thrown away."""
    async def mx(domain):
        return True
    monkeypatch.setattr(contacts, "has_mx", mx)

    import app.pipeline.runner as runner_mod
    real_write_outputs = runner_mod.write_outputs
    calls = {"n": 0}

    def flaky_write_outputs(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated export failure")
        return real_write_outputs(*a, **kw)
    monkeypatch.setattr(runner_mod, "write_outputs", flaky_write_outputs)

    with respx.mock:
        _mock_sites()
        deps = Deps(FakeSerper(), FakeLLM(), Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
                    [FakeFinder()], ActiveRegistry())
        summary = asyncio.run(run_job(_spec(), tmp_path / "out", deps=deps, today=TODAY))

    assert calls["n"] == 2  # first attempt raised; the failure path retried and succeeded
    assert summary.status == JobStatus.FAILED
    assert summary.outputs  # the salvage export actually ran and produced files
    assert summary.qualified + summary.no_contact >= 1  # acme/bravo were processed before the crash
    with session_scope() as s:
        job = s.get(Job, summary.job_id)
        assert job.status == JobStatus.FAILED and "simulated export failure" in job.error
        # The leads that succeeded before the crash are durable, not lost: already saved per-candidate.
        statuses = [l.status for l in s.query(JobLead).filter(JobLead.job_id == summary.job_id).all()]
        assert LeadStatus.QUALIFIED in statuses


def test_concurrent_company_insert_recovers_instead_of_raising(clean_db):
    """Two sessions racing to insert the same new domain: the loser must recover, never raise."""
    from sqlalchemy import func, select

    from app.models.tables import Company

    with session_scope() as other:  # the "other process" that wins the race
        other.add(Company(domain="race.co.uk", country_code="GB"))

    with session_scope() as mine:
        calls = {"n": 0}
        real_scalar = mine.scalar

        def flaky_scalar(stmt, *a, **kw):
            calls["n"] += 1
            # Simulate our own lookup missing the row the other session just committed.
            return None if calls["n"] == 1 else real_scalar(stmt, *a, **kw)
        mine.scalar = flaky_scalar

        company = store.upsert_company(mine, "race.co.uk", "GB")  # must not raise IntegrityError
        assert company.domain == "race.co.uk"

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Company).where(Company.domain == "race.co.uk")) == 1


def test_erase_person_deletes_contact_and_suppresses_email():
    from app.models.tables import Company, Contact

    with session_scope() as s:
        company = Company(domain="acme.co.uk", country_code="GB")
        s.add(company)
        s.flush()
        s.add(Contact(company_id=company.id, first_name="Jane", last_name="Roe", job_title="Head of Ops",
                      person_source_url="u", person_source_quote="q", email="jane.roe@acme.co.uk",
                      email_verdict=EmailVerdict.VALID, email_verified_at=datetime.now(UTC)))

    with session_scope() as s:
        erased = store.erase_person_by_email(s, "Jane.Roe@Acme.co.uk", reason="unsubscribe request")
    assert erased == 1

    with session_scope() as s:
        assert s.query(Contact).filter(Contact.email == "jane.roe@acme.co.uk").first() is None
        assert "jane.roe@acme.co.uk" in store.suppressed_emails(s)


def _all_rows(wb, sheet):
    values = list(wb[sheet].values)
    return [dict(zip(values[0], r)) for r in values[1:]]


@respx.mock
def test_icp_only_tier_is_opt_in_labelled_and_only_fills_the_gap(tmp_path: Path, clean_db, monkeypatch):
    """charlie's only signal is from 2024. With the tier on and an active registry record it is delivered on
    its own sheet (fit 35 >= 32), labelled icp_only, with no outreach draft (no genuine dated hook)."""
    async def mx(domain):
        return True
    monkeypatch.setattr(contacts, "has_mx", mx)
    _mock_sites()
    spec = _spec().model_copy(update={"include_icp_only": True})
    deps = Deps(FakeSerper(), FakeLLM(), Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
                [FakeFinder()], ActiveRegistry())
    summary = asyncio.run(run_job(spec, tmp_path / "out", deps=deps, today=TODAY))
    assert summary.qualified == 1 and summary.icp_only == 1, (summary, summary.rejected)

    wb = openpyxl.load_workbook(summary.outputs["xlsx"])
    icp = _all_rows(wb, "ICP fit - no strong signal")
    assert icp and {r["company_domain"] for r in icp} == {"charlie.co.uk"}
    assert all(r["lead_tier"] == "icp_only" and not r.get("email_subject") for r in icp)
    assert not icp[0]["signal_1_type"]  # the 2024 signal is outside the window and is not shown as current
    assert {r["company_domain"] for r in _all_rows(wb, "Leads")} == {"acme.co.uk"}
    with session_scope() as s:
        tiers = {(l.company_id, l.lead_tier) for l in s.query(JobLead).filter(JobLead.status != LeadStatus.REJECTED)}
        assert {t for _, t in tiers} == {LeadTier.SIGNAL, LeadTier.ICP_ONLY}


@respx.mock
def test_same_registered_company_on_two_domains_is_one_lead(tmp_path: Path, clean_db, monkeypatch):
    """All three sites print the same company number: they are one registered company, researched once."""
    async def mx(domain):
        return True
    monkeypatch.setattr(contacts, "has_mx", mx)
    monkeypatch.setitem(COMPANY_NUMBERS, "bravo.co.uk", "01234567")
    monkeypatch.setitem(COMPANY_NUMBERS, "charlie.co.uk", "01234567")
    _mock_sites()
    deps = Deps(FakeSerper(), FakeLLM(), Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
                [FakeFinder()], ActiveRegistry())
    summary = asyncio.run(run_job(_spec(), tmp_path / "out", deps=deps, today=TODAY))
    assert summary.rejected.get("duplicate company") == 2, summary.rejected
    with session_scope() as s:
        assert s.query(Company).filter(Company.registry_id == "01234567").count() == 1


@respx.mock
def test_company_delivered_under_one_domain_is_not_redelivered_under_another(tmp_path: Path, clean_db, monkeypatch):
    async def mx(domain):
        return True
    monkeypatch.setattr(contacts, "has_mx", mx)
    with session_scope() as s:  # an earlier job delivered company 01234567 (as acme.com) to this client
        job = Job(client_key="test-client", client_name="n", ask="a", target_countries=["GB"], requested_count=1,
                  cost_cap_inr=50.0, status=JobStatus.DONE)
        s.add(job)
        s.flush()
        company = Company(domain="acme.com", country_code="GB", registry_name="companies_house", registry_id="01234567")
        s.add(company)
        s.flush()
        s.add(Delivery(client_key="test-client", company_id=company.id, contact_id=None, job_id=job.id))
    _mock_sites()
    deps = Deps(FakeSerper(), FakeLLM(), Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
                [FakeFinder()], ActiveRegistry())
    summary = asyncio.run(run_job(_spec(), tmp_path / "out", deps=deps, today=TODAY))
    assert summary.rejected.get("duplicate company") == 1, summary.rejected  # acme.co.uk prints 01234567
    wb = openpyxl.load_workbook(summary.outputs["xlsx"])
    delivered = {r["company_domain"] for sheet in ("Leads", "Good leads - no contact") for r in _all_rows(wb, sheet)}
    assert "acme.co.uk" not in delivered and "bravo.co.uk" in delivered


def test_resolve_company_merges_only_on_a_printed_company_number(clean_db):
    from app.countries.base import REGISTRY_MATCH_NAME, REGISTRY_MATCH_NUMBER
    with session_scope() as s:
        s.add(Company(domain="acme.co.uk", country_code="GB", registry_name="companies_house", registry_id="01234567"))
    reg = {"registry_name": "companies_house", "registry_id": "01234567"}
    with session_scope() as s:
        by_number = store.resolve_company(s, "acme.com", "GB", reg | {"match": REGISTRY_MATCH_NUMBER})
        assert by_number.domain == "acme.co.uk"  # second domain resolves to the existing company
        by_name = store.resolve_company(s, "acme-brand.com", "GB", reg | {"match": REGISTRY_MATCH_NAME})
        assert by_name.domain == "acme-brand.com"  # a name-only match never merges records


def test_judge_sees_the_playbook_signal_strengths():
    from app.pipeline.extract import Extraction
    from app.pipeline.score import ScoreOut, score

    class CaptureLLM:
        data = ""

        async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
            CaptureLLM.data = data
            return ScoreOut(fit_score=30, intent_score=0, timing_score=5, why_this_lead="", outreach_angle="",
                            risks="", cited_evidence_ids=[])

    asyncio.run(score(Extraction("Acme", "brand", False), _spec(), CaptureLLM(), TODAY))
    assert '"signal_playbook"' in CaptureLLM.data and '"strength": "medium"' in CaptureLLM.data
