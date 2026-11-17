"""LinkedIn job ads and the agency job boards (Workable, Recruitee, Teamtailor). HTTP mocked, no network."""

import asyncio
from datetime import date

import httpx
import pytest
import respx

from app.providers.jobs.ats_client import AtsClient, parse_board_url
from app.providers.jobs.job_ad import JobAd
from app.providers.jobs.linkedin_client import (
    SEARCH_URL,
    LinkedInJobsClient,
    LinkedInRateLimitedError,
    parse_cards,
    parse_description,
)
from app.schemas.job_spec_schema import JobSpec, SourceSpec
from app.services import source_service

TODAY = date(2026, 11, 17)


def card(job_id, title, company, slug, posted="2026-11-13", where="Leicester, England, United Kingdom"):
    time = f'<time class="job-search-card__listdate" datetime="{posted}">4 days ago</time>' if posted else ""
    return f"""<li><div class="base-card base-search-card job-search-card"
      data-entity-urn="urn:li:jobPosting:{job_id}">
      <div class="base-search-card__info">
        <h3 class="base-search-card__title"> {title} </h3>
        <h4 class="base-search-card__subtitle"><a href="https://uk.linkedin.com/company/{slug}?trk=x"> {company} </a></h4>
        <div class="base-search-card__metadata"><span class="job-search-card__location"> {where} </span>{time}</div>
      </div></div></li>"""


DETAIL = '<div class="show-more-less-html__markup"> We are a digital agency in Leicester. <b>Hiring</b> a developer. </div>'


def test_cards_parse_with_exact_dates_and_undated_cards_are_skipped():
    ads = parse_cards(
        card(1, "Junior Frontend Developer", "Williams Commerce", "williams-commerce-ltd")
        + card(2, "No date", "Somebody", "somebody", posted=None)
    )
    [ad] = ads
    assert (ad.job_id, ad.employer_name, ad.employer_id) == (
        "1",
        "Williams Commerce",
        "williams-commerce-ltd",
    )
    assert ad.posted_on == date(2026, 11, 13)  # from <time datetime>, never from "4 days ago"
    assert ad.url == "https://www.linkedin.com/jobs/view/1"
    assert ad.board == "LinkedIn"
    assert parse_description(DETAIL) == "We are a digital agency in Leicester. Hiring a developer."
    assert parse_cards("") == []


@respx.mock
def test_search_asks_for_the_freshness_window_and_a_429_stops_linkedin_for_the_run():
    route = respx.get(SEARCH_URL).mock(
        side_effect=[httpx.Response(200, text=card(1, "Dev", "A", "a")), httpx.Response(429)]
    )

    async def go():
        li = LinkedInJobsClient(pause_s=0)
        try:
            first = await li.search("web developer", "United Kingdom", days=30)
            with pytest.raises(LinkedInRateLimitedError):
                await li.search("web developer", "United Kingdom", days=30)
            with pytest.raises(LinkedInRateLimitedError):  # no more requests once blocked
                await li.search("other", "United Kingdom", days=30)
            return first
        finally:
            await li.aclose()

    assert len(asyncio.run(go())) == 1
    assert route.calls[0].request.url.params["f_TPR"] == "r2592000"
    assert route.call_count == 2


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
SRC = SourceSpec(type="job_ads", keywords=["web developer agency"], category="digital agency")


def ad(job_id, employer, board="LinkedIn", posted=date(2026, 11, 13), description="", title="Web Developer"):
    return JobAd(str(job_id), "", employer, title, "Leeds", posted, description, f"https://x/{job_id}", board)


class FakeBoard:
    def __init__(self, ads, details=None):
        self.ads, self.details, self.blocked, self.described = ads, details or {}, False, []

    async def search(self, *args, **kwargs):
        return self.ads

    async def describe(self, a):
        self.described.append(a.job_id)
        return JobAd(**{**a.__dict__, "description": self.details.get(a.job_id, "We are an agency.")})


def run(clients, spec=SPEC):
    return asyncio.run(source_service._job_ad_candidates(SRC, spec, clients, TODAY))


def test_one_employer_on_both_boards_is_one_candidate_with_both_ads():
    reed = FakeBoard([ad(1, "Kensa Creative Ltd", board="Reed", description="Join our studio.")])
    linkedin = FakeBoard([ad(2, "Kensa Creative")])
    [cand] = run({"reed": reed, "linkedin": linkedin})
    assert cand.triage_note.startswith("2 job ad(s) on LinkedIn and Reed")
    assert {h.query.split(":")[0] for h in cand.seed_hits} == {"reed", "linkedin"}
    assert linkedin.described == ["2"]  # only LinkedIn ads need their text fetched


def test_a_linkedin_ad_that_reads_like_a_recruiter_drops_that_employer():
    """Live 2026-11-17: Searchability's ad read '...opportunity with an established, award-winning agency'."""
    linkedin = FakeBoard(
        [ad(1, "Searchability"), ad(2, "Williams Commerce")],
        details={"1": "Junior Web Developer opportunity with an established, award-winning digital agency"},
    )
    assert [c.name_hint for c in run({"linkedin": linkedin})] == ["Williams Commerce"]


def test_linkedin_alone_serves_countries_reed_does_not_cover():
    us = SPEC.model_copy(update={"countries": ["US"]})
    reed = FakeBoard([ad(1, "UK Only Studio", board="Reed")])
    linkedin = FakeBoard([ad(2, "Austin Agency")])
    assert [c.name_hint for c in run({"reed": reed, "linkedin": linkedin}, us)] == ["Austin Agency"]


def test_a_rate_limited_linkedin_keeps_the_other_boards_ads():
    class Blocked(FakeBoard):
        async def search(self, *args, **kwargs):
            raise LinkedInRateLimitedError("429")

    reed = FakeBoard([ad(1, "Pixel Agency", board="Reed", description="Join our studio.")])
    assert [c.name_hint for c in run({"reed": reed, "linkedin": Blocked([])})] == ["Pixel Agency"]


# --- agency job boards -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://apply.workable.com/eventogy/j/9EF3582CCC/", ("workable", "eventogy")),
        ("https://apply.workable.com/j/9EF3582CCC", None),  # a job link without the company
        ("https://tether.recruitee.com/o/head-of-systems", ("recruitee", "tether")),
        ("https://pixelstudio.teamtailor.com/jobs/123-dev", ("teamtailor", "pixelstudio")),
        ("https://career.teamtailor.com/jobs/1", None),  # Teamtailor's own careers site
        ("https://www.recruitee.com/blog", None),
    ],
)
def test_agency_board_urls(url, expected):
    assert parse_board_url(url) == expected


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel><title>Pixel Studio</title>
<item><title>WordPress Developer</title><description>&lt;p&gt;We build sites for brands.&lt;/p&gt;</description>
<pubDate>Wed, 11 Nov 2026 17:34:36 +0100</pubDate><link>https://pixelstudio.teamtailor.com/jobs/1-wp</link>
<tt:locations><tt:location><tt:city>Leeds</tt:city><tt:country>United Kingdom</tt:country></tt:location></tt:locations>
</item></channel></rss>"""


@respx.mock
def test_workable_recruitee_and_teamtailor_postings_are_dated_and_named():
    respx.get("https://apply.workable.com/api/v1/widget/accounts/eventogy").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Eventogy",
                "jobs": [
                    {
                        "title": "Developer",
                        "shortcode": "AB",
                        "url": "https://apply.workable.com/j/AB",
                        "published_on": "2026-11-10",
                        "city": "London",
                        "country": "United Kingdom",
                        "description": "<p>Build things</p>",
                    }
                ],
            },
        )
    )
    respx.get("https://tether.recruitee.com/api/offers/").mock(
        return_value=httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "title": "Engineer",
                        "company_name": "Tether",
                        "published_at": "2026-11-16 16:33:49 UTC",
                        "status": "published",
                        "location": "London, England, United Kingdom",
                        "careers_url": "https://tether.recruitee.com/o/engineer",
                        "description": "<p>Hello</p>",
                    }
                ]
            },
        )
    )
    respx.get("https://pixelstudio.teamtailor.com/jobs.rss").mock(return_value=httpx.Response(200, text=RSS))

    async def go():
        ats = AtsClient()
        try:
            return [
                await ats.postings(p, s)
                for p, s in (("workable", "eventogy"), ("recruitee", "tether"), ("teamtailor", "pixelstudio"))
            ]
        finally:
            await ats.client.aclose()

    [wk], [rc], [tt] = asyncio.run(go())
    assert (wk.company_name, wk.posted_on, wk.location) == (
        "Eventogy",
        date(2026, 11, 10),
        "London, United Kingdom",
    )
    assert (rc.company_name, rc.posted_on) == ("Tether", date(2026, 11, 16))
    assert (tt.company_name, tt.posted_on, tt.location) == (
        "Pixel Studio",
        date(2026, 11, 11),
        "Leeds, United Kingdom",
    )
    assert tt.text == "We build sites for brands."


@respx.mock
def test_a_teamtailor_feed_with_entities_is_not_expanded():
    evil = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]><rss><channel><title>&e;</title></channel></rss>'
    respx.get("https://evil.teamtailor.com/jobs.rss").mock(return_value=httpx.Response(200, text=evil))

    async def go():
        ats = AtsClient()
        try:
            return await ats.postings("teamtailor", "evil")
        finally:
            await ats.client.aclose()

    assert asyncio.run(go()) == []


def test_board_searches_cover_the_agency_platforms_and_name_the_kind_of_company():
    src = SourceSpec(
        type="ats_jobs",
        search_queries=['site:jobs.lever.co "developer"'],
        role_keywords=["developer"],
        locations=["United Kingdom"],
        category="digital agency",
    )
    qs = source_service.board_queries(src)
    assert any("site:apply.workable.com" in q and '"agency"' in q for q in qs)
    assert any("site:jobs.lever.co" in q and '"agency"' in q for q in qs)
    assert qs[1].endswith('"developer" "agency" "United Kingdom"')  # targeted top-ups before broad ones


def test_a_site_linking_to_its_workable_board_proves_the_website():
    assert source_service._links_to_board(
        '<a href="https://apply.workable.com/eventogy/">Jobs</a>', "workable", "eventogy"
    )
    assert source_service._links_to_board(
        '<a href="https://pixel.teamtailor.com">Careers</a>', "teamtailor", "pixel"
    )


def test_an_employer_with_more_ads_than_a_small_company_has_is_left_out():
    big = [ad(i, "Capgemini", board="Reed", description="Join us.") for i in range(41)]
    small = [ad(100, "Pixel Agency", board="Reed", description="Join our studio.")]
    assert [c.name_hint for c in run({"reed": FakeBoard(big + small)})] == ["Pixel Agency"]
