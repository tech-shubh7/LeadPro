"""The company's own website, never a page about it (job 29, 2026-11-19): "Brave Bison Commerce" resolved to
roastbrief.us, a publisher that reposted the news of its appointment. The lookup keyed on "commerce" (the
longest word, but a generic one) and accepted the article because its headline contained it. Offline."""

import asyncio
from datetime import date

import pytest

from app.providers.search.serper_client import SearchHit
from app.services.discovery_service import _distinctive_token, _is_site_page, resolve_domain
from app.services.extraction_service import ExtractOut, extract
from app.services.research_service import ResearchBundle
from tests.test_signal_intelligence import SPEC

ARTICLE = "https://roastbrief.us/pjh-appoints-brave-bison-commerce-to-lead-b2b-ecommerce-transformation/"


@pytest.mark.parametrize(
    ("name", "token"),
    [
        ("Brave Bison Commerce", "brave"),
        ("Gradin Digital Agency", "gradin"),
        ("Brightwave Robotics", "brightwave"),
        ("Digital Agency", None),  # only generic words: no lookup rather than a wrong one
    ],
)
def test_the_name_is_looked_up_by_its_identifying_word(name, token):
    assert _distinctive_token(name) == token


def test_an_article_url_is_not_a_site_page():
    assert _is_site_page("https://bravebison.com/")
    assert _is_site_page("https://www.example.com/about-us")
    assert not _is_site_page(ARTICLE)
    assert not _is_site_page("https://example.com/en/news/item")


class JobTwentyNineSerper:
    """What the lookup saw: a publisher's article ranked above the company's own site."""

    async def query(self, q, kind="search", gl="uk", num=10, tbs=None):
        return [
            SearchHit(
                "PJH Appoints Brave Bison Commerce to Lead B2B Ecommerce Transformation",
                ARTICLE,
                "PJH has appointed Brave Bison Commerce ...",
                None,
                "search",
                q,
            ),
            SearchHit(
                "Brave Bison | Social-first media and commerce",
                "https://bravebison.com/",
                "Brave Bison is a new-age media and marketing company",
                None,
                "search",
                q,
            ),
        ]


def test_the_lookup_skips_the_article_and_finds_the_company_site():
    assert asyncio.run(
        resolve_domain(JobTwentyNineSerper(), "Brave Bison Commerce", "us", "ecommerce agency")
    ) == ("bravebison.com")


class OnlyTheArticle:
    async def query(self, q, kind="search", gl="uk", num=10, tbs=None):
        return [SearchHit("PJH Appoints Brave Bison Commerce", ARTICLE, "s", None, "search", q)]


def test_no_site_found_is_better_than_a_publisher():
    assert asyncio.run(resolve_domain(OnlyTheArticle(), "Brave Bison Commerce", "us")) is None


class Capture:
    extract_model = judge_model = "m"

    def __init__(self):
        self.instructions = ""

    async def parse(self, model, instructions, data, schema, **kw):
        self.instructions = instructions
        return ExtractOut(
            sources_match_target=True,
            company_name="x",
            company_description="d",
            is_recruitment_or_staffing_firm=False,
            facts=[],
            signals=[],
            people=[],
        )


def test_the_reader_is_told_a_publisher_is_not_the_company_site():
    llm = Capture()
    asyncio.run(
        extract(
            ResearchBundle("roastbrief.us", None, target_name="Brave Bison Commerce"),
            SPEC,
            llm,
            date(2026, 11, 19),
        )
    )
    assert "news site, blog or directory" in llm.instructions
