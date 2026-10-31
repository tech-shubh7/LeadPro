"""A job is described by a YAML file (hand-written ICP + signal playbook for pilots)."""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class ClientSpec(BaseModel):
    key: str  # stable slug; never-repeat suppression is per client key
    name: str
    website: str | None = None


class SenderSpec(BaseModel):
    """Who the outreach is from. `why_choose` must be true, checkable statements: drafts may use only these
    (plus the prospect's verified evidence), never invented credentials."""
    name: str
    offer: str  # one line: what is sold
    why_choose: list[str] = Field(default_factory=list)
    signature: str = ""


class SeedSpec(BaseModel):
    """A company nominated by outside research. It still goes through every check (live fetch, quotes,
    dates, registry, verified email); seeding only decides what gets researched."""
    domain: str
    name: str
    category: str | None = None
    note: str = ""


class SignalSpec(BaseModel):
    type: str
    description: str
    queries: list[str]
    kind: str = "search"  # "search" or "news"
    # How directly this event implies a need for the client's offer (e.g. an RFP = very_high, generic news = low).
    strength: Literal["very_high", "high", "medium", "low"] = "medium"


class Playbook(BaseModel):
    signals: list[SignalSpec]
    buyer_titles: list[str]
    negative: list[str] = Field(default_factory=list)


class JobSpec(BaseModel):
    client: ClientSpec
    ask: str
    countries: list[str] = ["GB"]
    count: int = Field(gt=0, le=500)
    freshness_days: int = 90
    personalized_email: bool = False
    # Opt-in tier: companies that fit the ICP (registry-active, high fit) but show no current dated signal.
    # Delivered on their own sheet, clearly labelled, only to fill up to `count` after signal leads.
    include_icp_only: bool = False
    cost_cap_inr: float | None = None
    icp: dict
    playbook: Playbook
    suppress_domains: list[str] = Field(default_factory=list)
    sender: SenderSpec | None = None
    seeds: list[SeedSpec] = Field(default_factory=list)
    seeds_only: bool = False  # research only the seeds (outside research already found them); skip discovery
    max_search_queries: int = Field(default=30, ge=1, le=300)  # discovery budget (planned + resolution)

    @field_validator("countries")
    @classmethod
    def upper(cls, v: list[str]) -> list[str]:
        if len(v) != 1:
            raise ValueError("one country per job for now (run separate jobs per country)")
        return [c.upper() for c in v]


def load_job(path: Path) -> JobSpec:
    return JobSpec.model_validate(yaml.safe_load(path.read_text()))
