"""Reed job ads as a signal-first source: direct employers hiring contractors. HTTP mocked, no network."""

import asyncio
import base64
from datetime import date

import httpx
import pytest
import respx

from app.providers.jobs.reed_client import BASE_URL, ReedClient, parse_job, parse_reed_date
from app.schemas.job_spec_schema import JobSpec, SourceSpec
from app.services import source_service

TODAY = date(2026, 11, 17)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("14/11/2026", date(2026, 11, 14)),
        ("2026-11-14", date(2026, 11, 14)),
        ("2026-11-14T09:30:00", date(2026, 11, 14)),
        ("/Date(1794787200000)/", date(2026, 11, 16)),
        ("not a date", None),
        (None, None),
    ],
)
def test_reed_dates_in_every_likely_format(value, expected):
    assert parse_reed_date(value) == expected


def test_a_result_parses_in_either_casing_and_one_missing_essentials_is_skipped():
    camel = parse_job(
        {"jobId": 1, "employerName": "Pixel Agency", "jobTitle": "Freelance Developer", "date": "14/11/2026"}
    )
    pascal = parse_job({"JobId": 2, "EmployerName": "Pixel Agency", "JobTitle": "Contract Dev"})
    assert camel.employer_name == "Pixel Agency"
    assert camel.posted_on == date(2026, 11, 14)
    assert camel.url == "https://www.reed.co.uk/jobs/1"
    assert pascal.job_id == "2"
    assert parse_job({"jobId": 3, "jobTitle": "No employer"}) is None


@respx.mock
def test_search_authenticates_and_asks_for_direct_employer_ads_of_any_contract_type(monkeypatch):
    from app.config.settings import get_settings

    monkeypatch.setattr(get_settings(), "reed_api_key", "k")
    route = respx.get(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "jobId": 1,
                        "employerName": "Pixel Agency",
                        "jobTitle": "Freelance Developer",
                        "date": "14/11/2026",
                    }
                ]
            },
        )
    )

    async def go():
        reed = ReedClient()
        try:
            return await reed.search("freelance developer", location="Leeds")
        finally:
            await reed.aclose()

    [job] = asyncio.run(go())
    request = route.calls.last.request
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"k:").decode()
    assert request.url.params["postedByDirectEmployer"] == "true"
    assert "contract" not in request.url.params  # contract ads on Reed are almost all recruiters'
    assert request.url.params["locationName"] == "Leeds"
    assert job.title == "Freelance Developer"


class FakeReed:
    def __init__(self, jobs):
        self.jobs = jobs

    async def search(self, keywords, location="", contract=True, limit=100):
        return self.jobs


def _job(
    job_id, employer, title="Freelance Web Developer", posted="2026-11-10", description="Join our studio."
):
    return parse_job(
        {
            "jobId": job_id,
            "employerName": employer,
            "jobTitle": title,
            "date": posted,
            "jobDescription": description,
        }
    )


SPEC = JobSpec.model_validate(
    {
        "client": {"key": "k", "name": "K"},
        "ask": "UK agencies",
        "count": 5,
        "icp": {},
        "freshness_days": 30,
        "playbook": {"signals": [], "buyer_titles": ["md"]},
    }
)
SRC = SourceSpec(type="job_ads", keywords=["freelance web developer"], category="digital agency")


def test_job_ads_become_one_dated_candidate_per_employer_without_recruiters_or_stale_ads():
    jobs = [
        _job(1, "Pixel Agency"),
        _job(2, "Pixel Agency", "Contract React Developer", "2026-11-12"),
        _job(3, "Bright Recruitment Ltd"),  # recruiter by name
        _job(
            4, "Acme Talent", description="Our client is a fast-growing agency in Leeds"
        ),  # recruiter by wording
        _job(5, "Old Studio", posted="2026-08-01"),  # outside the 30-day window
        _job(6, "Undated Studio", posted=None),
    ]
    [cand] = asyncio.run(source_service._job_ad_candidates(SRC, SPEC, {"reed": FakeReed(jobs)}, TODAY))
    assert cand.name_hint == "Pixel Agency"
    assert [h.kind for h in cand.seed_hits] == ["job_ad", "job_ad"]
    assert cand.seed_hits[0].date_text == "2026-11-12"  # newest first
    assert cand.category == "digital agency"


def test_a_job_ad_is_dated_and_labelled_as_a_job_posting():
    from app.models.enums import DateSource
    from app.services.extraction_service import _signal_date
    from app.services.research_service import ResearchBundle
    from app.services.signal_intelligence_service import source_class

    hit = source_service._job_ad_hit(_job(1, "Pixel Agency"), TODAY)
    bundle = ResearchBundle("pixel.co.uk", None, seed_hits=[hit])
    assert _signal_date(hit.link, bundle, TODAY).source == DateSource.JOB_POSTING
    assert source_class(hit.link, "pixel.co.uk", {hit.link: hit.kind}) == "job_posting"


def test_without_any_job_board_the_source_is_skipped_not_failed():
    assert asyncio.run(source_service._job_ad_candidates(SRC, SPEC, {}, TODAY)) == []


def test_the_planner_job_ad_source_is_used_in_every_country_linkedin_covers():
    from app.services.intake_service import DraftedJobAdsSource, planned_sources

    class Out:
        job_board_source = contract_awards_source = None
        browse_sources = ()
        job_ads_source = DraftedJobAdsSource(keywords=["freelance web developer"], category="agency", why="w")

    assert [s["type"] for s in planned_sources(Out(), "GB")] == ["job_ads"]
    assert [s["type"] for s in planned_sources(Out(), "US")] == ["job_ads"]


@pytest.mark.parametrize(
    "employer",
    [
        "ITOL Recruit",
        "eFinancialCareers",
        "Acme Resourcing Ltd",
        "Top Talent UK",
        "Prime Personnel",
        "Reed Jobs",
    ],
)
def test_recruiters_and_job_boards_posing_as_employers_are_dropped(employer):
    """Live Reed data: these came back flagged as direct employers."""
    assert source_service._is_recruiter_ad(_job(9, employer))


def test_a_real_agency_is_not_mistaken_for_a_recruiter():
    assert not source_service._is_recruiter_ad(_job(9, "Kensa Creative"))
