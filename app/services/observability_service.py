"""Where the money and tokens went: roll-ups of a run's usage ledger (Job.cost_lines) for the Trace tab and the
workspace Observability page. Works for every run, old ones included: lines saved before tagging show under
"untagged", and lines saved before the token split show totals without the split."""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.models import Job

MODEL_PROVIDERS = ("openai", "gemini")


@dataclass
class Bucket:
    name: str
    calls: int = 0
    tokens: int = 0
    input: int = 0
    cached: int = 0
    output: int = 0
    reasoning: int = 0
    cost_inr: float = 0.0
    split_cost_inr: float = 0.0  # cost of the model calls that recorded their input/output split

    def add(self, line: dict) -> None:
        self.calls += 1
        self.cost_inr += float(line.get("cost_inr") or 0.0)
        if line.get("provider") in MODEL_PROVIDERS:
            self.tokens += int(line.get("units") or 0)
            self.input += int(line.get("input_tokens") or 0)
            self.cached += int(line.get("cached_tokens") or 0)
            self.output += int(line.get("output_tokens") or 0)
            self.reasoning += int(line.get("reasoning_tokens") or 0)
            if line.get("input_tokens") or line.get("output_tokens"):
                self.split_cost_inr += float(line.get("cost_inr") or 0.0)


@dataclass
class Usage:
    total: Bucket = field(default_factory=lambda: Bucket("total"))
    models: Bucket = field(default_factory=lambda: Bucket("model calls"))
    by_stage: list[Bucket] = field(default_factory=list)
    by_model: list[Bucket] = field(default_factory=list)  # model calls only
    by_provider: list[Bucket] = field(default_factory=list)  # every paid service
    cache_hits: int = 0  # model answers reused from the cache (free)

    @property
    def has_split(self) -> bool:
        return bool(self.models.input or self.models.output)

    @property
    def other_inr(self) -> float:
        return self.total.cost_inr - self.models.cost_inr

    @property
    def output_share_of_spend(self) -> float | None:
        """Share of model spend that paid for output (incl. reasoning). Counted only over calls that recorded
        the split: older calls have a total and a price but no split, and would dilute the share to nothing."""
        if not self.has_split or not self.models.split_cost_inr:
            return None
        settings = get_settings()
        spent_out = 0.0
        for b in self.by_model:
            _, _, p_out = settings.price_for(b.name)
            spent_out += b.output * p_out / 1_000_000 * settings.usd_to_inr
        return min(1.0, spent_out / self.models.split_cost_inr)


def usage(lines: list[dict] | None) -> Usage:
    out = Usage()
    stages: dict[str, Bucket] = {}
    models: dict[str, Bucket] = {}
    providers: dict[str, Bucket] = {}
    for line in lines or []:
        provider = line.get("provider") or "?"
        if provider == "cache":
            out.cache_hits += 1
            continue
        out.total.add(line)
        providers.setdefault(provider, Bucket(provider)).add(line)
        stages.setdefault(line.get("stage") or "untagged", Bucket(line.get("stage") or "untagged")).add(line)
        if provider in MODEL_PROVIDERS:
            out.models.add(line)
            name = line.get("operation") or "?"
            models.setdefault(name, Bucket(name)).add(line)
    by_cost = lambda b: -b.cost_inr  # noqa: E731
    out.by_stage = sorted(stages.values(), key=by_cost)
    out.by_model = sorted(models.values(), key=by_cost)
    out.by_provider = sorted(providers.values(), key=by_cost)
    return out


@dataclass
class RunRow:
    job: Job
    usage: Usage


def workspace(session: Session, limit: int = 50) -> tuple[Usage, list[RunRow]]:
    """Every run's ledger summed, and the latest runs one by one."""
    jobs = list(session.scalars(select(Job).order_by(Job.id.desc())))
    everything = usage([line for job in jobs for line in (job.cost_lines or [])])
    return everything, [RunRow(job, usage(job.cost_lines)) for job in jobs[:limit]]


def configuration() -> list[tuple[str, str]]:
    """What the run is set up to do, shown beside the numbers (like a status sidebar)."""
    s = get_settings()
    judge, cheap = s.llm_judge_model, s.llm_extract_model
    if s.llm_mode == "gemini":
        judge, cheap = s.gemini_judge_model, s.gemini_extract_model
    return [
        ("Model provider", s.llm_mode),
        ("Plan drafting", judge),
        ("Scoring", judge),
        ("Search-query planning", cheap),
        ("Search triage, page reading, intent", cheap),
        ("Email drafts (contacts mode)", cheap),
        ("Qualify bar / Watchlist", f"{s.qualify_score} / {s.watchlist_min_score}–{s.qualify_score - 1}"),
        (
            "Page text shown to the model",
            f"{s.llm_max_page_chars:,} chars dated, {s.llm_supporting_page_chars:,} other",
        ),
        ("Model answer cache", f"{s.llm_cache_days} days" if s.llm_cache_days else "off"),
        ("Research check", "on" if s.research_check else "off"),
        ("Call tracing", f"on, kept {s.trace_keep_days} days" if s.trace_llm_calls else "off"),
    ]
