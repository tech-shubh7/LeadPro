import asyncio
from datetime import date

import pytest
from pydantic import ValidationError

from app.models.enums import DateSource
from app.providers.jobs.ats_client import JobPosting, parse_board_url, plain_text
from app.providers.search.serper_client import SearchHit
from app.providers.tenders.contracts_finder_client import parse_release
from app.schemas.job_spec_schema import JobSpec, SourceSpec
from app.services.extraction_service import _signal_date, build_sources
from app.services.research_service import ResearchBundle
from app.services.source_service import ats_hit, collect_sources, notice_hit

TODAY = date(2026, 11, 1)


def test_parse_board_url_only_known_platforms():
    assert parse_board_url("https://jobs.lever.co/acme/123-abc") == ("lever", "acme")
    assert parse_board_url("https://job-boards.greenhouse.io/acme/jobs/9") == ("greenhouse", "acme")
    assert parse_board_url("https://boards.greenhouse.io/embed/job_app?for=acme") == ("greenhouse", "acme")
    assert parse_board_url("https://boards.greenhouse.io/embed/job_app") is None
    assert parse_board_url("https://jobs.ashbyhq.com/Acme-Co/uuid") == ("ashby", "Acme-Co")
    assert parse_board_url("https://jobs.smartrecruiters.com/AcmeLtd/1") == ("smartrecruiters", "AcmeLtd")
    assert parse_board_url("https://apply.workable.com/acme/") is None  # no verified public endpoint
    assert parse_board_url("https://evil.example/jobs.lever.co/acme") is None


def test_plain_text_unescapes_greenhouse_content():
    assert plain_text("&lt;p&gt;Grow our &lt;b&gt;pipeline&lt;/b&gt;&lt;/p&gt;") == "Grow our pipeline"


def posting(
    title="Business Development Manager", loc="London, UK", days_ago=10, slug="acme", name="Acme Digital"
):
    return JobPosting(
        "lever",
        slug,
        name,
        title,
        "Sales",
        loc,
        f"https://jobs.lever.co/{slug}/{title[:3]}",
        date(2026, 11, 1 - 0) if days_ago == 0 else date.fromordinal(TODAY.toordinal() - days_ago),
        "We need a BDM to build pipeline.",
    )


class FakeSerper:
    def __init__(self, links):
        self.links, self.calls = links, 0

    async def query(self, q, **kw):
        self.calls += 1
        return [SearchHit("t", link, "", None, "search", q) for link in self.links]


class FakeAts:
    def __init__(self, boards):
        self.boards = boards

    async def postings(self, platform, slug):
        return self.boards.get(slug, [])

    async def with_text(self, p):
        return p


def spec_with(**src):
    base = {
        "type": "ats_jobs",
        "search_queries": ["q1"],
        "role_keywords": ["business development", "sales"],
        "locations": ["London", "UK"],
        "category": "digital agency",
    }
    return JobSpec.model_validate(
        {
            "client": {"key": "k", "name": "K"},
            "ask": "x",
            "count": 5,
            "freshness_days": 90,
            "icp": {},
            "playbook": {"signals": [], "buyer_titles": []},
            "sources": [base | src],
            "sources_only": True,
        }
    )


def collect(spec, serper, ats, cf=None):
    return asyncio.run(
        collect_sources(spec, serper, {"ats": ats, "contracts": cf or object()}, "uk", TODAY, 10)
    )


def test_ats_filters_role_location_and_age():
    boards = {
        "acme": [posting(), posting("Sales Executive", days_ago=3), posting("Chef", days_ago=1)],
        "oldco": [posting(days_ago=200, slug="oldco", name="Old Co")],
        "nyc": [posting(loc="New York, US", slug="nyc", name="Nyc Co")],
        "uker": [posting(loc="Remote - UK", slug="uker", name="Uker")],
    }
    serper = FakeSerper([f"https://jobs.lever.co/{s}/x" for s in boards] + ["https://random.com/x"])
    cands, used = collect(spec_with(), serper, FakeAts(boards))
    names = {c.name_hint for c in cands}
    assert names == {"Acme Digital", "Uker"}  # stale, wrong-place and non-sales boards dropped
    from app.services.source_service import board_queries

    assert (
        used == len(board_queries(spec_with().sources[0])) == serper.calls
    )  # plan query + per-platform top-up
    acme = next(c for c in cands if c.name_hint == "Acme Digital")
    assert len(acme.seed_hits) == 2
    assert all(h.kind == "ats" for h in acme.seed_hits)
    assert acme.category == "digital agency"
    assert acme.prior > 0.8


def test_ats_min_roles():
    boards = {"acme": [posting()]}
    cands, _ = collect(spec_with(min_roles=2), FakeSerper(["https://jobs.lever.co/acme/x"]), FakeAts(boards))
    assert cands == []


def test_search_budget_respected():
    serper = FakeSerper(["https://jobs.lever.co/acme/x"])
    _, used = asyncio.run(
        collect_sources(
            spec_with(search_queries=["a", "b", "c"]),
            serper,
            {"ats": FakeAts({}), "contracts": object()},
            "uk",
            TODAY,
            2,
        )
    )
    assert used == 2
    assert serper.calls == 2


RELEASE = {
    "date": "2026-10-30T18:09:39Z",
    "buyer": {"name": "Cabinet Office"},
    "tender": {
        "title": "Website rebuild",
        "description": "Rebuild the\r\n public website",
        "classification": {"id": "72000000"},
    },
    "parties": [
        {
            "id": "GB-COH-06404381",
            "name": "Mondas Consulting Limited",
            "roles": ["supplier"],
            "identifier": {"scheme": "GB-COH", "id": "6404381"},
            "details": {"scale": "sme"},
        },
        {"id": "S2", "name": "No Number Ltd", "roles": ["supplier"], "details": {"scale": "large"}},
    ],
    "awards": [
        {
            "date": "2026-10-22T00:00:00+01:00",
            "datePublished": "2026-10-30T18:09:39Z",
            "value": {"amount": 290000},
            "suppliers": [
                {"id": "GB-COH-06404381", "name": "Mondas Consulting Limited"},
                {"id": "S2", "name": "No Number Ltd"},
            ],
            "documents": [
                {
                    "documentType": "awardNotice",
                    "url": "https://www.contractsfinder.service.gov.uk/Notice/abc",
                }
            ],
        }
    ],
}


def test_parse_release_extracts_number_scale_and_dates():
    a, b = parse_release(RELEASE)
    assert a.supplier_number == "06404381"
    assert a.scale == "sme"
    assert b.supplier_number is None
    assert a.awarded_on == date(2026, 10, 22)
    assert a.published_on == date(2026, 10, 30)
    assert "Award date: 22 October 2026" in a.text
    assert "£290,000" in a.text
    assert "\r" not in a.description


class FakeCF:
    async def awards(self, frm, to, max_pages):
        return parse_release(RELEASE)


def test_award_candidates_sme_only_and_carry_number():
    spec = spec_with(
        type="contracts_awards", keywords=["website"], search_queries=[], role_keywords=[], locations=[]
    )
    cands, used = collect(spec, FakeSerper([]), FakeAts({}), FakeCF())
    assert [c.name_hint for c in cands] == ["Mondas Consulting Limited"]
    assert used == 0
    assert cands[0].registry_id == "06404381"
    assert cands[0].seed_hits[0].kind == "notice"


def test_source_spec_requires_its_fields():
    with pytest.raises(ValidationError):
        SourceSpec(type="ats_jobs", role_keywords=["sales"])
    with pytest.raises(ValidationError):
        SourceSpec(type="contracts_awards")


def bundle_with(hit, page_html=None):
    return ResearchBundle("acme.co.uk", None, seed_hits=[hit])


def test_first_party_hits_are_dated_by_the_api_and_quoted_from_the_record():
    p = posting()
    hit = ats_hit(p, TODAY)
    d = _signal_date(hit.link, bundle_with(hit), TODAY)
    assert d.value == p.posted_on
    assert d.source == DateSource.JOB_POSTING
    _, texts = build_sources(bundle_with(hit))
    assert "We need a BDM to build pipeline." in texts[hit.link]
    nh = notice_hit(parse_release(RELEASE)[0], TODAY)
    nd = _signal_date(nh.link, bundle_with(nh), TODAY)
    assert nd.value == date(2026, 10, 30)
    assert nd.source == DateSource.OFFICIAL_NOTICE


def test_a_large_employer_board_is_not_a_candidate():
    """Job 13 researched Databricks and GitLab for an agency client: they hire in-house."""
    boards = {
        "acme": [posting()],
        "bigco": [posting(slug="bigco", name="Big Co", title=f"Sales Executive {i}") for i in range(60)],
    }
    serper = FakeSerper(["https://jobs.lever.co/acme", "https://jobs.lever.co/bigco"])
    found, _ = collect(spec_with(), serper, FakeAts(boards))
    assert [c.name_hint for c in found] == ["Acme Digital"]


def test_board_searches_reach_every_platform_and_the_new_greenhouse_host():
    """Job 15: two plan queries, one on Greenhouse's retired host, found two boards."""
    from app.services.source_service import MAX_BOARD_QUERIES, board_queries

    src = spec_with(
        search_queries=['site:boards.greenhouse.io "frontend developer"'],
        role_keywords=["developer", "engineer"],
        locations=["United Kingdom"],
    ).sources[0]
    qs = board_queries(src)
    assert "job-boards.greenhouse.io" in qs[0]
    assert len(qs) <= MAX_BOARD_QUERIES
    assert any("jobs.ashbyhq.com" in q for q in qs)
    assert any("jobs.lever.co" in q for q in qs)


# --- proving a job-board company's website ---------------------------------------------------------

from app.providers.web.page_fetcher import FetchResult  # noqa: E402
from app.services.source_service import board_checker_for  # noqa: E402


class PageFetcher:
    def __init__(self, pages):  # url -> html (None = unreadable)
        self.pages = pages

    async def fetch(self, url, fresh=False):
        html = self.pages.get(url)
        if html is None:
            return FetchResult(url, url, 403, "", "", "now")
        return FetchResult(url, url, 200, html, "text", "now")


SARDINE_HOME = '<a href="https://jobs.lever.co/sardine">Careers</a>'
CAREERS_LINK = '<a href="/careers">Join us</a>'
SARDINE_CAREERS = '<iframe src="https://boards.greenhouse.io/embed/job_board?for=sardine"></iframe>'
INVESTOR_PAGE = "<h1>Sardine business breakdown</h1><a href='https://sardine.ai'>sardine.ai</a>"


@pytest.mark.parametrize(
    ("pages", "platform", "expected"),
    [
        ({"https://sardine.ai/": SARDINE_HOME}, "lever", True),
        (
            {"https://sardine.ai/": CAREERS_LINK, "https://sardine.ai/careers": SARDINE_CAREERS},
            "greenhouse",
            True,
        ),
        ({"https://sardine.ai/": INVESTOR_PAGE}, "lever", False),
        ({}, "lever", None),  # unreadable site: no proof either way
    ],
)
def test_a_company_site_is_proven_by_linking_to_its_own_job_board(pages, platform, expected):
    check = board_checker_for(PageFetcher(pages))
    assert asyncio.run(check("sardine.ai", platform, "sardine")) is expected


def test_a_job_board_candidate_gets_the_site_that_links_to_its_board():
    """Job 21: 'Sardine' resolved to contrary.com (an investor's page about Sardine)."""
    from app.providers.search.serper_client import SearchHit as Hit
    from app.services.discovery_service import Candidate, _resolved_domain

    class Serper:
        async def query(self, q, **kw):
            return [
                Hit("Sardine | Contrary Research", "https://contrary.com/sardine", "", None, "search", q),
                Hit("Sardine | Fraud prevention", "https://sardine.ai/", "", None, "search", q),
            ]

    pages = {"https://contrary.com/": INVESTOR_PAGE, "https://sardine.ai/": SARDINE_HOME}
    cand = Candidate(
        domain="",
        name_hint="Sardine",
        seed_hits=[
            Hit(
                "Sardine job posting", "https://jobs.lever.co/sardine/1", "", None, "ats", "ats:lever:sardine"
            )
        ],
    )
    assert (
        asyncio.run(_resolved_domain(Serper(), cand, "uk", board_checker_for(PageFetcher(pages))))
        == "sardine.ai"
    )
