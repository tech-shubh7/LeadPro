"""Continuing a run the budget stopped, and the model-answer cache that makes it cheap. Offline."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.config.database import session_scope
from app.models import Company, Delivery, Job, JobLead
from app.models.enums import JobStatus, LeadStatus
from app.providers.llm.llm_cache import CachingLLMProvider
from app.repositories import run_queue_repository as queue
from app.utils.cost_tracker import CostTracker


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


SPEC = {
    "client": {"key": "k", "name": "K"},
    "ask": "a",
    "count": 5,
    "icp": {},
    "cost_cap_inr": 50,
    "playbook": {"signals": [{"type": "t", "description": "d", "queries": ["q"]}], "buyer_titles": ["ceo"]},
}


class Out(BaseModel):
    answer: str


class CountingLLM:
    extract_model = judge_model = "m"

    def __init__(self):
        self.calls, self.cost = 0, CostTracker(cap_inr=10)

    async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
        self.calls += 1
        return schema(answer=f"answer to {data}")


def test_the_same_question_is_answered_once_and_then_reused_for_free(tmp_path):
    inner = CountingLLM()
    llm = CachingLLMProvider(inner, max_age_days=7, cache_dir=tmp_path)

    async def ask(data):
        return await llm.parse("m", "i", data, Out)

    first, again, other = asyncio.run(ask("acme")), asyncio.run(ask("acme")), asyncio.run(ask("brightwave"))
    assert first == again
    assert other.answer == "answer to brightwave"
    assert inner.calls == 2  # 'acme' was paid for once
    assert inner.cost.lines[-1].provider == "cache"


def _stopped_job(stopped_by_cap=True, domain="acme.co.uk") -> int:
    with session_scope() as s:
        job = Job(
            client_key="k",
            client_name="K",
            ask="a",
            target_countries=["GB"],
            requested_count=5,
            cost_cap_inr=50,
            cost_inr=49.5,
            status=JobStatus.DONE_WITH_SHORTFALL,
            run_spec=SPEC,
            progress={"stopped_by_cap": stopped_by_cap},
        )
        s.add(job)
        s.flush()
        company = Company(domain=domain, country_code="GB")
        s.add(company)
        s.flush()
        s.add(
            JobLead(
                job_id=job.id,
                company_id=company.id,
                status=LeadStatus.QUALIFIED_NO_CONTACT,
                lead_tier="signal",
                fit_score=30,
                intent_score=30,
                timing_score=15,
                total_score=75,
            )
        )
        s.add(Delivery(client_key="k", company_id=company.id, contact_id=None, job_id=job.id))
        return job.id


def test_a_run_stopped_by_its_budget_continues_with_a_bigger_one(clean_db):
    job_id = _stopped_job()
    assert queue.continue_with_budget(job_id, 120) is None
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert (job.status, job.cost_cap_inr, job.run_spec["cost_cap_inr"]) == (JobStatus.QUEUED, 120, 120)
        assert job.cost_inr == 49.5  # spend so far still counts against the new cap
        assert s.query(JobLead).count() == 0
        assert s.query(Delivery).count() == 0  # or its own leads would be skipped as already delivered


def test_only_a_budget_stop_can_be_continued_and_only_upwards(clean_db):
    finished = _stopped_job(stopped_by_cap=False)
    assert "reached its budget" in queue.continue_with_budget(finished, 120)
    capped = _stopped_job(domain="brightwave.co.uk")
    assert "more than the current" in queue.continue_with_budget(capped, 40)


def test_the_run_page_offers_to_continue_and_the_form_queues_it(clean_db, client):
    job_id = _stopped_job()
    page = client.get(f"/jobs/{job_id}").text
    assert "Continue with this budget" in page
    res = client.post(
        f"/jobs/{job_id}/continue",
        data={"csrf": client.cookies.get("lp_csrf"), "cost_cap_inr": 100},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with session_scope() as s:
        assert s.get(Job, job_id).status == JobStatus.QUEUED
