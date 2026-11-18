"""A job is described by a YAML file (hand-written ICP + signal playbook for pilots)."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import OutputMode


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
    # Articles found by outside research. They are fetched live like any other source; only the fetched page
    # text can be quoted (the note above is never evidence).
    source_urls: list[str] = Field(default_factory=list)


# Hard ceiling on one job's spend, whatever is typed: a stray extra zero must not be spent.
MAX_COST_CAP_INR = 5000.0


class SourceSpec(BaseModel):
    """A structured public dataset in which every row is already a candidate buyer (a company hiring for a
    role, a supplier that just won a contract). Candidates found this way still pass every downstream check;
    the source only decides who gets researched, replacing noisy general web search."""

    type: Literal["ats_jobs", "contracts_awards", "agent_browse", "job_ads"]
    category: str | None = (
        None  # what these companies are ("digital agency"); keeps the website lookup on target
    )
    # ats_jobs: Greenhouse / Lever / Ashby / SmartRecruiters public job APIs
    search_queries: list[str] = Field(
        default_factory=list
    )  # Google queries that surface job boards, e.g. site:jobs.lever.co ...
    role_keywords: list[str] = Field(
        default_factory=list
    )  # a posting counts if its title has one of these as whole words
    locations: list[str] = Field(
        default_factory=list
    )  # posting location must contain one as a whole word; empty = any
    min_roles: int = Field(default=1, ge=1)
    max_boards: int = Field(default=60, ge=1, le=300)
    # A board (or job_ads employer) with more open roles than this belongs to a large company (Databricks,
    # GitLab, Capgemini), which hires in-house rather than buying from an agency. Raise it only when the ICP
    # really is enterprise.
    max_open_roles: int = Field(default=40, ge=1, le=10000)
    # contracts_awards: UK Contracts Finder award notices
    keywords: list[str] = Field(default_factory=list)  # any of these in the notice title/description
    cpv_prefixes: list[str] = Field(default_factory=list)  # e.g. "72" = IT services
    sme_only: bool = True  # supplier declared itself an SME in the notice
    published_days: int = Field(default=30, ge=1, le=90)
    max_pages: int = Field(default=10, ge=1, le=40)
    # job_ads: contract-only is off by default because, on Reed, contract roles are almost all posted by
    # recruiters (who hide the employer); direct employers mostly advertise permanent roles.
    contract_only: bool = False
    # agent_browse: a browser agent reads one public page (an accelerator cohort, an exhibitor list, a
    # directory of new firms) and lists the companies on it. Discovery only: every lead it suggests is
    # re-fetched and quote-checked by our own fetcher like any other.
    url: str | None = None
    goal: str | None = None  # what to collect from the page, in plain words
    max_steps: int = Field(default=12, ge=1, le=25)  # each step is billed: this is the spend cap per page
    max_companies: int = Field(default=15, ge=1, le=40)

    @model_validator(mode="after")
    def needs_its_fields(self):
        if self.type == "ats_jobs" and not (self.search_queries and self.role_keywords):
            raise ValueError("ats_jobs needs search_queries and role_keywords")
        if self.type == "contracts_awards" and not (self.keywords or self.cpv_prefixes):
            raise ValueError("contracts_awards needs keywords or cpv_prefixes")
        if self.type == "job_ads" and not self.keywords:
            raise ValueError("job_ads needs keywords (the job-ad searches to run)")
        if self.type == "agent_browse" and not (self.url and self.goal):
            raise ValueError("agent_browse needs url and goal")
        return self


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
    freshness_days: int = Field(default=90, ge=1, le=365)  # the jobs table enforces the same range
    personalized_email: bool = False
    # Opt-in tier: companies that fit the ICP (registry-active, high fit) but show no current dated signal.
    # Delivered on their own sheet, clearly labelled, only to fill up to `count` after signal leads.
    include_icp_only: bool = False
    cost_cap_inr: float | None = Field(default=None, gt=0, le=MAX_COST_CAP_INR)
    icp: dict
    playbook: Playbook
    suppress_domains: list[str] = Field(default_factory=list)
    sender: SenderSpec | None = None
    seeds: list[SeedSpec] = Field(default_factory=list)
    seeds_only: bool = False  # research only the seeds (outside research already found them); skip discovery
    sources: list[SourceSpec] = Field(
        default_factory=list
    )  # structured public datasets, tried before web search
    sources_only: bool = False  # seeds + sources only; skip general web-search discovery
    max_search_queries: int = Field(default=30, ge=1, le=300)  # discovery budget (planned + resolution)
    # SIGNALS_ONLY delivers company + dated buying signal and nothing about people: no contact lookup, no
    # paid email finders, no personal data stored, no drafts. Contacts come from the client's own tools.
    output_mode: OutputMode = OutputMode.CONTACTS
    # What the client sells, in one line (the plan's offer). Kept on the spec so every step judges intent
    # against it, also in a signals-only run, which has no sender.
    offer: str = ""
    # The client sells capacity to companies like the targets (white-label or outsourced development, staff
    # augmentation): a target's own job posting for that work counts as demand, labelled "hiring for this
    # skill", instead of as evidence it builds in-house. See signal_intelligence_service.HIRING_FOR_OFFER.
    hiring_is_demand: bool = False

    @field_validator("countries")
    @classmethod
    def upper(cls, v: list[str]) -> list[str]:
        if len(v) != 1:
            raise ValueError("one country per job for now (run separate jobs per country)")
        return [c.upper() for c in v]

    @model_validator(mode="after")
    def _signals_only_is_consistent(self) -> "JobSpec":
        """A signals-only run has no people to write to and delivers only companies WITH a dated signal,
        so drafts and the no-signal ICP tier contradict it. Rejected here rather than silently dropped."""
        if self.output_mode == OutputMode.SIGNALS_ONLY:
            if self.personalized_email:
                raise ValueError("signals_only delivers no contacts, so personalized_email must be off")
            if self.include_icp_only:
                raise ValueError(
                    "signals_only delivers only companies with a dated signal: include_icp_only must be off"
                )
        return self

    @property
    def wants_contacts(self) -> bool:
        return self.output_mode == OutputMode.CONTACTS


def load_job(path: Path) -> JobSpec:
    return JobSpec.model_validate(yaml.safe_load(path.read_text()))
