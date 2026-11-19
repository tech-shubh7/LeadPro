"""The research check: proof that selective reading costs fewer tokens without losing genuine leads.

Switched on for a proof run only (RESEARCH_CHECK=true). For every company, the run's own selective reading
is compared with the old full reading of the SAME fetched pages:
  - a company the date check skipped is read in full anyway: did any fresh signal exist?
  - a company read selectively is also read in full: did the full reading find a fresh signal on a page
    the selective one did not, and would the company's outcome have differed?
The full reading's calls are tagged stage "check_*", so the report separates them from the run's own cost.

Both readings are model outputs, so they can differ by chance on identical input; a difference is listed
for a person to look at, not counted as proof of a loss on its own.
"""

from dataclasses import asdict, dataclass, field
from datetime import date

from app.config.settings import get_settings
from app.schemas.job_spec_schema import JobSpec
from app.services.extraction_service import Extraction, build_sources, extract
from app.services.research_service import ResearchBundle
from app.services.scoring_service import fresh_signals, on_watchlist, score
from app.services.signal_intelligence_service import assess_signals, can_qualify, combine
from app.utils.cost_tracker import usage_tags

SKIPPED = "skipped free"  # the selective arm's outcome when the date check rejected the company


@dataclass
class CheckResult:
    domain: str
    gated: bool
    selective_chars: int  # characters the run's own (selective) reading showed the model
    full_chars: int  # characters the old full reading showed the model
    full_fresh: list[dict] = field(default_factory=list)  # fresh signals the full reading found
    selective_fresh: list[dict] = field(default_factory=list)
    full_outcome: str = ""
    full_score: int | None = None
    selective_outcome: str = ""  # set once the run has decided this company
    selective_score: int | None = None

    @property
    def lost_urls(self) -> list[str]:
        """Pages with a fresh signal in the full reading and none in the selective one."""
        kept = {s["url"] for s in self.selective_fresh}
        return sorted({s["url"] for s in self.full_fresh} - kept)

    def as_dict(self) -> dict:
        return asdict(self) | {"lost_urls": self.lost_urls}


def _fresh(ext: Extraction, spec: JobSpec, today: date) -> list[dict]:
    return [
        {"url": s.source_url, "date": s.effective_date.isoformat(), "quote": s.quote[:300]}
        for s in fresh_signals(ext, today, spec.freshness_days)
    ]


async def _full_outcome(ext: Extraction, bundle: ResearchBundle, spec: JobSpec, llm, today: date):
    """The verdict the full reading leads to: the same intent and score steps the run uses, without the
    checks that do not depend on how much was read (registry, website identity)."""
    if not fresh_signals(ext, today, spec.freshness_days):
        return "no fresh signal", None
    kinds = {hit.link: hit.kind for hit in bundle.seed_hits}
    with usage_tags(stage="check_intent"):
        assessed = await assess_signals(ext.signals, spec, ext.company_name, bundle.domain, kinds, llm)
    ext.signals, ext.against = assessed.buying, assessed.against
    qualifying = [s for s in fresh_signals(ext, today, spec.freshness_days) if can_qualify(s)]
    if not qualifying:
        return "no qualifying signal", None
    with usage_tags(stage="check_score"):
        sc = await score(ext, spec, llm, today, combine(qualifying).summary)
    if sc.total >= get_settings().qualify_score:
        return "lead", sc.total
    return ("watchlist" if on_watchlist(sc, spec) else "rejected"), sc.total


async def check(
    bundle: ResearchBundle, spec: JobSpec, llm, today: date, selective: Extraction | None
) -> CheckResult:
    """Reads the same pages in full and records how it compares. `selective` is None when the date check
    skipped the company (no selective reading happened)."""
    full_data, _ = build_sources(bundle)
    shown = len(build_sources(bundle, today, spec.freshness_days, spec.wants_contacts)[0]) if selective else 0
    with usage_tags(stage="check_extract"):
        full = await extract(bundle, spec, llm, today, selective=False)
    result = CheckResult(
        bundle.domain,
        gated=selective is None,
        selective_chars=shown,
        full_chars=len(full_data),
        full_fresh=_fresh(full, spec, today),
        selective_fresh=_fresh(selective, spec, today) if selective else [],
    )
    if selective is None:
        result.selective_outcome = SKIPPED
    result.full_outcome, result.full_score = await _full_outcome(full, bundle, spec, llm, today)
    return result


def report(job) -> dict:
    """What the research check of one job shows: tokens of each reading and every possible loss."""
    rows = (job.progress or {}).get("research_check") or []
    tokens: dict[str, float] = {}
    for line in job.cost_lines or []:
        if line.get("provider") in ("openai", "gemini") and line.get("stage"):
            tokens[line["stage"]] = tokens.get(line["stage"], 0) + float(line.get("units") or 0)
    selective = sum(tokens.get(s, 0) for s in ("extract", "intent", "score"))
    full = sum(tokens.get(s, 0) for s in ("check_extract", "check_intent", "check_score"))
    gated = [r for r in rows if r["gated"]]
    return {
        "companies": len(rows),
        "skipped_free": len(gated),
        "skipped_with_fresh_signal": [r["domain"] for r in gated if r["full_fresh"]],
        "pages_with_lost_signal": {r["domain"]: r["lost_urls"] for r in rows if r["lost_urls"]},
        "outcome_changed": [
            {
                "domain": r["domain"],
                "selective": r["selective_outcome"],
                "full": r["full_outcome"],
                "selective_score": r["selective_score"],
                "full_score": r["full_score"],
            }
            for r in rows
            if _verdict(r["selective_outcome"]) != _verdict(r["full_outcome"])
        ],
        "selective_chars": sum(r["selective_chars"] for r in rows),
        "full_chars": sum(r["full_chars"] for r in rows),
        "selective_tokens": int(selective),
        "full_tokens": int(full),
        "tokens_by_stage": {k: int(v) for k, v in sorted(tokens.items())},
    }


def _verdict(outcome: str) -> str:
    """Lead and watchlist are kept; everything else (rejected for any reason, skipped) is not."""
    return outcome if outcome in ("lead", "watchlist") else "not kept"
