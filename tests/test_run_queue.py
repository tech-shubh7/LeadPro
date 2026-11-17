"""The run queue: leases, crash recovery, attempt limits, and one owner per run (real Postgres)."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from app.config.database import session_scope
from app.models import Company, Evidence, Job, JobLead
from app.models.enums import EvidenceKind, JobStatus, LeadStatus
from app.repositories import run_queue_repository as queue
from app.services import run_worker_service
from app.services.run_worker_service import RunWorker

SPEC = {
    "client": {"key": "k", "name": "K"},
    "ask": "a",
    "count": 1,
    "icp": {},
    "playbook": {"signals": [{"type": "t", "description": "d", "queries": ["q"]}], "buyer_titles": ["ceo"]},
}


def _job(run_spec=SPEC, status=JobStatus.QUEUED) -> int:
    with session_scope() as s:
        job = Job(
            client_key="k",
            client_name="K",
            ask="a",
            target_countries=["GB"],
            requested_count=1,
            cost_cap_inr=10,
            status=status,
            run_spec=run_spec,
        )
        s.add(job)
        s.flush()
        return job.id


def _expire(job_id: int) -> None:
    with session_scope() as s:
        s.get(Job, job_id).locked_until = datetime.now(UTC) - timedelta(seconds=1)


def test_a_leased_run_is_not_claimed_twice_until_its_lease_expires(clean_db):
    job_id = _job()
    assert queue.claim("w1", 60, 3) == (job_id, SPEC, 1)
    assert queue.claim("w2", 60, 3) is None  # w1 holds it
    _expire(job_id)  # w1 crashed: no heartbeat
    assert queue.claim("w2", 60, 3) == (job_id, SPEC, 2)
    assert not queue.heartbeat(job_id, "w1", 60)  # w1 lost it; w2 owns it now
    assert queue.heartbeat(job_id, "w2", 60)


def test_a_run_started_from_the_cli_is_never_taken_over(clean_db):
    _job(run_spec=None, status=JobStatus.RESEARCHING)
    assert queue.claim("w1", 60, 3) is None


def test_eight_workers_racing_get_exactly_one_owner(clean_db):
    _job()
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: queue.claim(f"w{i}", 60, 3), range(8)))
    assert sum(r is not None for r in results) == 1


def test_a_run_out_of_attempts_is_failed_with_the_reason(clean_db):
    job_id = _job()
    for attempt in range(3):
        assert queue.claim(f"w{attempt}", 60, 3)
        _expire(job_id)
    assert queue.claim("w9", 60, 3) is None
    assert queue.give_up_exhausted(3) == [job_id]
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.FAILED
        assert "after 3 attempts" in job.error


def test_a_graceful_stop_does_not_spend_an_attempt(clean_db):
    job_id = _job()
    queue.claim("w1", 60, 3)
    queue.requeue(job_id, "w1")
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert (job.status, job.attempts, job.locked_by) == (JobStatus.QUEUED, 0, None)


def test_a_retry_starts_from_a_clean_slate_for_that_run(clean_db):
    job_id = _job()
    with session_scope() as s:
        company = Company(domain="acme.co.uk", country_code="GB")
        s.add(company)
        s.flush()
        s.add(JobLead(job_id=job_id, company_id=company.id, status=LeadStatus.REJECTED, reject_reason="r"))
        s.add(
            Evidence(
                company_id=company.id,
                job_id=job_id,
                kind=EvidenceKind.FIRMOGRAPHIC,
                claim="c",
                source_url="https://acme.co.uk",
                quote="q",
                quote_verified=True,
            )
        )
    queue.clear_partial_results(job_id)
    with session_scope() as s:
        assert s.query(JobLead).count() == 0
        assert s.query(Evidence).count() == 0
        assert s.query(Company).count() == 1  # shared across runs: kept


def test_the_worker_runs_a_queued_run_and_releases_it(clean_db, monkeypatch):
    job_id = _job()
    ran = []

    async def fake_run_job(spec, out_dir, job_id):
        ran.append((job_id, spec.ask))

    monkeypatch.setattr(run_worker_service, "run_job", fake_run_job)

    async def main():
        worker = RunWorker(concurrency=1, name="test")
        await worker.tick()
        await asyncio.gather(*worker._running.values())

    asyncio.run(main())
    assert ran == [(job_id, "a")]
    with session_scope() as s:
        assert s.get(Job, job_id).locked_by is None


def test_stopping_the_worker_puts_its_run_back_in_the_queue(clean_db, monkeypatch):
    job_id = _job()

    async def slow_run_job(spec, out_dir, job_id):
        await asyncio.sleep(60)

    monkeypatch.setattr(run_worker_service, "run_job", slow_run_job)

    async def main():
        worker = RunWorker(concurrency=1, name="test")
        await worker.tick()
        await asyncio.sleep(0.05)
        await worker.stop()

    asyncio.run(main())
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert (job.status, job.attempts, job.locked_by) == (JobStatus.QUEUED, 0, None)
