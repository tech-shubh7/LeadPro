"""Planner evals: realistic client scenarios through the REAL planning model, repeated, with a pass rate.

    uv run python -m scripts.eval_planner --repeats 3            # all scenarios
    uv run python -m scripts.eval_planner --only brightpath_uk     # one scenario

Each scenario fixes the client's site text (so only the model varies) and states what a correct plan
must and must not contain. Every plan also passes the general quality checks. A failure prints the exact
reason; the report is saved under evals/reports/ so runs can be compared over time.
"""

import argparse
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.providers.llm.llm_factory import create_llm
from app.providers.web.page_fetcher import FetchResult
from app.services.eligibility_service import PROCUREMENT
from app.services.intake_service import Plan, draft_plan
from app.services.plan_quality_service import check_plan
from app.utils.cost_tracker import CostTracker
from app.utils.logger import configure_logging

REPORTS = Path("evals/reports")
Check = Callable[[Plan], str | None]  # returns why it failed, or None


def _queries(plan: Plan) -> list[str]:
    return [q for s in plan.signals for q in s.get("queries", [])]


def no_tenders(plan: Plan) -> str | None:
    bad = [q for q in _queries(plan) if PROCUREMENT.search(q)]
    bad += [
        s.get("url", "")
        for s in plan.sources
        if PROCUREMENT.search(f"{s.get('url', '')} {s.get('goal', '')}")
    ]
    return f"tender searches for an ineligible client: {bad}" if bad else None


def uses_tenders(plan: Plan) -> str | None:
    if any(PROCUREMENT.search(q) for q in _queries(plan)):
        return None
    return "an eligible public-sector supplier got no tender search"


def located(country: str) -> Check:
    return lambda plan: (
        None
        if plan.profile.country == country
        else (f"client located in {plan.profile.country or 'unknown'}, expected {country}")
    )


def mentions(field_name: str, *words: str) -> Check:
    def check(plan: Plan) -> str | None:
        text = json.dumps(getattr(plan, field_name)).lower()
        return None if any(w in text for w in words) else f"{field_name} mentions none of {words}"

    return check


@dataclass
class Scenario:
    name: str
    website: str
    site_text: str
    ask: str
    country: str
    checks: list[Check] = field(default_factory=list)


SCENARIOS = [
    Scenario(
        "brightpath_uk",
        "brightpathweb-example.com",
        "Brightpath Web is a web and mobile app development company based in Pune, India, since 2008. We "
        "deliver custom web development, Laravel, Shopify, WordPress and AI automation for clients in the UK, "
        "USA and Australia, and white-label development for digital agencies. Call +91 79 4006 1111.",
        "UK companies asking for proposals for a new website, app or automation work",
        "GB",
        [no_tenders, located("IN")],
    ),
    Scenario(
        "brightpath_white_label",
        "brightpathweb-example.com",
        "Brightpath Web is a web and mobile app development company based in Pune, India. We are a "
        "white-label development partner for digital agencies in the UK and USA. Call +91 79 4006 1111.",
        "UK web design and marketing agencies that need a white-label or overflow development partner",
        "GB",
        [no_tenders, located("IN"), mentions("icp", "agenc")],
    ),
    Scenario(
        "uk_council_supplier",
        "acme-digital.co.uk",
        "Acme Digital, 12 Park Row, Leeds LS1 5HD. We build accessible websites for the public sector and "
        "are trusted by Leeds City Council, Bradford Council and the Cabinet Office. We are on the G-Cloud "
        "framework. Call 0113 496 0000.",
        "UK councils and public bodies that need a new accessible website",
        "GB",
        [uses_tenders, located("GB")],
    ),
    Scenario(
        "us_hr_saas",
        "peoplepilot.io",
        "PeoplePilot is HR software for US companies with 50-500 employees: onboarding, payroll sync and "
        "performance reviews. Headquartered in Austin, Texas. SOC 2 Type II.",
        "US mid-size companies growing their headcount fast",
        "US",
        [no_tenders, mentions("buyer_titles", "hr", "people", "talent")],
    ),
    Scenario(
        "uk_3pl",
        "northfulfil-example.co.uk",
        "North Fulfilment is a third-party logistics warehouse in Wakefield, West Yorkshire. We pick, pack "
        "and ship for e-commerce brands selling 500 to 20,000 orders a month.",
        "UK e-commerce brands that are outgrowing their own fulfilment",
        "GB",
        [no_tenders, mentions("icp", "commerce", "brand", "d2c")],
    ),
    Scenario(
        "india_manufacturer",
        "sunrisebox-example.in",
        "Sunrise Box Co, MIDC Bhosari, Pune, Maharashtra. Manufacturer of corrugated boxes and custom "
        "printed packaging for FMCG and pharma companies across India.",
        "Indian FMCG and pharma companies launching new products",
        "IN",
        [no_tenders, located("IN")],
    ),
]


class FixtureFetcher:
    """Serves the scenario's site text as the client's homepage, so only the model varies."""

    def __init__(self, website: str, text: str):
        self.url, self.text = f"https://{website}/", text

    async def fetch(self, url: str) -> FetchResult:
        now = datetime.now(UTC).isoformat()
        if url.rstrip("/") == self.url.rstrip("/"):
            return FetchResult(url, url, 200, f"<html><body><p>{self.text}</p></body></html>", self.text, now)
        return FetchResult(url, url, 404, "", "", now)


async def run_once(scenario: Scenario, mode: str | None) -> list[str]:
    llm = create_llm(CostTracker(cap_inr=50), mode, cache=False)  # evals measure variance
    plan = await draft_plan(
        scenario.website,
        scenario.ask,
        5,
        scenario.country,
        llm,
        FixtureFetcher(scenario.website, scenario.site_text),
    )
    if not plan.generated:
        return [f"model did not answer: {plan.note}"]
    failures = [f"{i.check}: {i.detail}" for i in check_plan(plan)]
    failures += [msg for check in scenario.checks if (msg := check(plan))]
    return failures


def _save_report(report: dict) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"planner_{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(report, indent=1))
    return out


async def main(repeats: int, only: str | None, mode: str | None, concurrency: int) -> int:
    scenarios = [s for s in SCENARIOS if not only or s.name == only]
    sem = asyncio.Semaphore(concurrency)

    async def guarded(s: Scenario) -> tuple[str, list[str]]:
        async with sem:
            return s.name, await run_once(s, mode)

    results = await asyncio.gather(*(guarded(s) for s in scenarios for _ in range(repeats)))
    report: dict[str, dict] = {}
    for name, failures in results:
        entry = report.setdefault(name, {"runs": 0, "passed": 0, "failures": []})
        entry["runs"] += 1
        entry["passed"] += not failures
        entry["failures"] += failures
    total = sum(e["runs"] for e in report.values())
    passed = sum(e["passed"] for e in report.values())
    for name, e in report.items():
        print(f"{'PASS' if e['passed'] == e['runs'] else 'FAIL'}  {name:24} {e['passed']}/{e['runs']}")
        for failure in dict.fromkeys(e["failures"]):
            print(f"        - {failure[:200]}")
    print(f"\n{passed}/{total} plans passed ({100 * passed / max(total, 1):.0f}%)")
    out = await asyncio.to_thread(_save_report, {"passed": passed, "total": total, "scenarios": report})
    print(f"report: {out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", default=None)
    parser.add_argument("--llm", default=None, help="openai | gemini | manual (default: LLM_MODE)")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    configure_logging("WARNING")
    raise SystemExit(asyncio.run(main(args.repeats, args.only, args.llm, args.concurrency)))
