"""The model reads selectively, and never at the cost of a qualifying signal.

Found in jobs 26-27 (2026-11-18): 7 lakh OpenAI tokens across five jobs, 91% of them the extraction step
reading every fetched page in full (homepage, up to 8 site pages, up to 5 outside pages, 15,000 characters
each), including for undated web candidates that were then rejected for having no dated signal. A signal's
freshness comes only from code-trusted dates, so whether one is possible is known before the model reads
anything. Fully offline: no key, no network.
"""

import asyncio
import random
from datetime import date, timedelta

import httpx
import respx

from app.config.constants import NO_FRESH_SOURCE_REASON
from app.config.settings import get_settings
from app.models.enums import DateSource, OutputMode
from app.providers.search.serper_client import SearchHit
from app.providers.web.page_fetcher import Fetcher, FetchResult, PageCache
from app.services.discovery_service import Candidate
from app.services.extraction_service import (
    ExtractOut,
    QuotedSignal,
    build_sources,
    extract,
    fresh_source_urls,
    not_too_old,
    verified_extraction,
)
from app.services.research_service import ResearchBundle, research
from app.services.scoring_service import fresh_signals
from app.utils.dates import TrustedDate
from tests.test_signal_intelligence import SPEC

TODAY = date(2026, 11, 18)
DAYS = 60
FILLER = "Our studio builds websites and products for ambitious brands around the world. " * 120


def _page(url: str, published: str | None = None, body: str = FILLER) -> FetchResult:
    meta = f'<meta property="article:published_time" content="{published}">' if published else ""
    html = f"<html><head>{meta}</head><body><article><p>{body}</p></article></body></html>"
    return FetchResult(url, url, 200, html, body, "2026-11-18T00:00:00")


HOME = "https://agency.com/"
ABOUT = "https://agency.com/about"
LEGAL = "https://agency.com/privacy"
TEAM = "https://agency.com/team"
FRESH = "https://press.example/news/agency-seeks-partner"
STALE = "https://press.example/news/agency-2024"
UNDATED = "https://agency.com/blog/thoughts"


def _bundle(**pages: FetchResult) -> ResearchBundle:
    b = ResearchBundle("agency.com", pages.get("home"), target_name="Agency")
    roles = {"home": "home", "about": "about", "legal": "legal", "team": "team"}
    for key, page in pages.items():
        b.pages[page.final_url] = page
        if key in roles:
            b.page_roles[page.final_url] = roles[key]
    return b


def _full_bundle() -> ResearchBundle:
    return _bundle(
        home=_page(HOME),
        about=_page(ABOUT),
        legal=_page(LEGAL),
        team=_page(TEAM),
        fresh=_page(FRESH, "2026-11-10"),
        stale=_page(STALE, "2024-03-01"),
        undated=_page(UNDATED),
    )


# --- the gate ----------------------------------------------------------------------------------------


def test_only_sources_dated_inside_the_window_can_hold_a_fresh_signal():
    assert fresh_source_urls(_full_bundle(), TODAY, DAYS) == {FRESH}


def test_a_dated_job_ad_or_a_relative_search_date_counts_as_dated():
    b = _bundle(home=_page(HOME))
    b.seed_hits = [
        SearchHit(
            "Agency job posting",
            "https://www.linkedin.com/jobs/view/1",
            "Contract developer",
            "2026-11-17",
            "job_ad",
            "linkedin:1",
            TODAY.isoformat(),
        ),
        SearchHit(
            "Agency news",
            "https://news.example/x",
            "Agency hires",
            "3 days ago",
            "news",
            "q",
            TODAY.isoformat(),
        ),
    ]
    assert fresh_source_urls(b, TODAY, DAYS) == {
        "https://www.linkedin.com/jobs/view/1",
        "https://news.example/x",
    }


def test_the_gate_never_rejects_a_mis_dated_future_page():
    """A page dated in the future could still state a fresh event date; the gate must let it through."""
    assert not_too_old(TrustedDate(TODAY + timedelta(days=400), DateSource.ARTICLE_META), TODAY, DAYS)
    assert not not_too_old(
        TrustedDate(TODAY - timedelta(days=DAYS + 1), DateSource.ARTICLE_META), TODAY, DAYS
    )
    assert not not_too_old(None, TODAY, DAYS)


def test_no_fresh_signal_ever_comes_from_a_source_the_gate_calls_too_old():
    """The property the gate rests on, checked over many random pages, dates and stated event dates: every
    signal that ends up fresh was cited from a source in fresh_source_urls. If this ever failed, the gate
    could cost a real lead."""
    rng = random.Random(7)
    for _ in range(300):
        pages, signals = {}, []
        for i in range(4):
            url = f"https://press.example/news/item-{i}"
            published = (
                None if rng.random() < 0.3 else (TODAY + timedelta(days=rng.randint(-400, 60))).isoformat()
            )
            stated = (TODAY + timedelta(days=rng.randint(-400, 30))).strftime("%d %B %Y")
            quote = f"Agency announced item {i} needing outside development partners"
            pages[f"p{i}"] = _page(url, published, f"{quote}. Stated on {stated}. {FILLER[:200]}")
            signals.append(
                QuotedSignal(
                    claim=f"item {i}",
                    source_url=url,
                    quote=quote,
                    signal_type="x",
                    event_date_quote=f"{stated}",
                    event_key=f"e{i}",
                )
            )
        bundle = _bundle(home=_page(HOME), **pages)
        out = ExtractOut(
            sources_match_target=True,
            company_name="Agency",
            company_description="d",
            is_recruitment_or_staffing_firm=False,
            facts=[],
            signals=signals,
            people=[],
        )
        _, texts = build_sources(bundle)
        ext = verified_extraction(out, bundle, texts, TODAY)
        allowed = fresh_source_urls(bundle, TODAY, DAYS)
        assert {s.source_url for s in fresh_signals(ext, TODAY, DAYS)} <= allowed


# --- what the model reads ----------------------------------------------------------------------------


def _blocks(data: str) -> dict[str, str]:
    out = {}
    for block in data.split("<source url=")[1:]:
        url, _, rest = block.partition(">\n")
        out[url.strip('"')] = rest.rsplit("\n</source>", 1)[0]
    return out


def test_fresh_sources_and_the_homepage_are_read_in_full_and_the_rest_is_cut():
    s = get_settings()
    data, texts = build_sources(_full_bundle(), TODAY, DAYS, wants_people=False)
    shown = _blocks(data)
    assert len(shown[HOME]) == len(shown[FRESH]) == s.llm_max_page_chars
    for url in (ABOUT, STALE, UNDATED):
        assert len(shown[url]) == s.llm_supporting_page_chars
    assert LEGAL not in shown  # parsed for company numbers in code; never worth the model's tokens
    assert TEAM not in shown  # no contacts wanted: names are not read
    assert all(len(texts[u]) == len(FILLER) for u in (LEGAL, TEAM, STALE))  # quotes still checked in full


def test_a_team_page_is_read_whole_when_contacts_are_wanted():
    shown = _blocks(build_sources(_full_bundle(), TODAY, DAYS, wants_people=True)[0])
    assert len(shown[TEAM]) == get_settings().llm_max_page_chars


def test_without_a_date_window_every_source_is_shown_in_full_as_before():
    shown = _blocks(build_sources(_full_bundle())[0])
    assert set(shown) == {HOME, ABOUT, LEGAL, TEAM, FRESH, STALE, UNDATED}
    assert {len(v) for v in shown.values()} == {get_settings().llm_max_page_chars}


def test_the_selective_input_is_much_smaller():
    before = len(build_sources(_full_bundle())[0])
    after = len(build_sources(_full_bundle(), TODAY, DAYS, wants_people=False)[0])
    assert after < before * 0.6, (before, after)  # this fixture: 42,403 -> 21,298 characters


class CapturingLLM:
    extract_model = judge_model = "m"

    def __init__(self):
        self.instructions = ""

    async def parse(self, model, instructions, data, schema, **kw):
        self.instructions = instructions
        return ExtractOut(
            sources_match_target=True,
            company_name="Agency",
            company_description="d",
            is_recruitment_or_staffing_firm=False,
            facts=[],
            signals=[],
            people=[],
        )


def test_a_signals_only_run_asks_for_no_people():
    signals_only = SPEC.model_copy(update={"output_mode": OutputMode.SIGNALS_ONLY})
    llm = CapturingLLM()
    asyncio.run(extract(_full_bundle(), signals_only, llm, TODAY))
    assert "return an empty list" in llm.instructions
    asyncio.run(extract(_full_bundle(), SPEC, llm, TODAY))
    assert "only named individuals" in llm.instructions


# --- research ----------------------------------------------------------------------------------------

HOME_HTML = (
    '<html><body><a href="/about">About</a><a href="/team">Our team</a><a href="/privacy">Privacy</a>'
    f"<p>{FILLER[:300]}</p></body></html>"
)


@respx.mock
def test_research_labels_each_page_and_skips_team_pages_without_contacts(tmp_path):
    respx.get("https://agency.com/robots.txt").respond(404)
    respx.get(HOME).respond(200, html=HOME_HTML)
    respx.get(ABOUT).respond(200, html=f"<html><body><main><p>{FILLER[:400]}</p></main></body></html>")
    respx.get(LEGAL).respond(200, html=f"<html><body><main><p>{FILLER[:400]}</p></main></body></html>")
    team = respx.get(TEAM).respond(200, html="<html><body><p>Jane Roe, CEO</p></body></html>")

    async def go(wants_people):
        fetcher = Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / str(wants_people), ttl=None))
        return await research(Candidate("agency.com", "Agency"), fetcher, "GB", wants_people=wants_people)

    bundle = asyncio.run(go(False))
    assert not team.called
    assert bundle.page_roles == {HOME: "home", ABOUT: "about", LEGAL: "legal"}
    bundle = asyncio.run(go(True))
    assert team.called
    assert bundle.page_roles[TEAM] == "team"


# --- in a run ----------------------------------------------------------------------------------------


def test_in_a_run_an_undated_company_is_rejected_without_a_model_call(tmp_path, clean_db, monkeypatch):
    from tests import test_runner_e2e as e2e

    calls = []

    class CountingLLM(e2e.FakeLLM):
        async def parse(self, model, instructions, data, schema, effort="low", max_output_tokens=16000):
            if schema is ExtractOut:
                calls.append(instructions)
            return await super().parse(model, instructions, data, schema, effort, max_output_tokens)

    async def mx(domain):
        return True

    monkeypatch.setattr(e2e.contacts, "has_mx", mx)
    with respx.mock:
        e2e._mock_sites()
        deps = e2e.Deps(
            e2e.FakeSerper(),
            CountingLLM(),
            Fetcher(client=httpx.AsyncClient(), cache=PageCache(tmp_path / "c", ttl=None)),
            [e2e.FakeFinder()],
            e2e.FakeRegistry(),
        )
        summary = asyncio.run(e2e.run_job(e2e._spec(), tmp_path / "out", deps=deps, today=e2e.TODAY))
    assert summary.rejected == {NO_FRESH_SOURCE_REASON.format(days=90).split(" (")[0]: 1}
    assert not any("website is charlie.co.uk" in i for i in calls)  # its only article is from 2024
    assert any("website is acme.co.uk" in i for i in calls)
    assert summary.qualified == 1
