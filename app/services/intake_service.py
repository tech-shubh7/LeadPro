"""Step 0: turn a plain request into a research plan.

The operator gives three things: the client's website, who they want to reach, and how many. This module
reads the client's own site, then drafts the ICP and the signal playbook that the rest of the pipeline
runs on. The draft is always shown for review before anything is spent, because a wrong playbook is the
one error the downstream quote, date and email checks cannot catch.

With no LLM available it falls back to a generic, clearly-labelled template built from the request, so
the product still works and never pretends a template was researched.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.config.constants import COUNTRIES, SIGNAL_STRENGTHS
from app.exceptions.custom_exceptions import BadRequestError
from app.providers.web.page_fetcher import Fetcher
from app.repositories import client_repository
from app.schemas.client_profile_schema import ClientFactsOut, ClientProfile
from app.schemas.job_spec_schema import (
    MAX_COST_CAP_INR,
    ClientSpec,
    JobSpec,
    Playbook,
    SenderSpec,
    SignalSpec,
    SourceSpec,
)
from app.services.eligibility_service import apply_eligibility, verify_client_facts
from app.services.research_service import pick_internal_links
from app.utils.logger import get_logger
from app.utils.normalize import normalize_domain

logger = get_logger(__name__)

MAX_CLIENT_PAGES = 4
MAX_CHARS_PER_PAGE = 6000


# What counts as a buying signal, strongest first. The ranking is the point: a company that has said in
# public that it wants this work done is worth more than any number of companies we infer might need it.
# This text goes to the planner verbatim.
DEMAND_LADDER = """
Rank every signal you propose on this ladder, and spend your best queries at the top of it:

1. ASKING FOR IT (very_high). The company has publicly said it wants this work done by someone outside:
   a tender, an RFP, a "request for proposal", "seeking quotes", "inviting agencies", a brief posted to a
   marketplace, a contract-for-services advert, or a post asking for a recommended supplier or partner.
2. HIRING TO DO IT, with signs they cannot do it in-house yet (high). A vacancy for exactly the skill the
   client sells, especially contract/freelance/interim, an agency-side or outsourced role, a first-ever
   hire for that skill, or a role that has been open a long time. A big in-house team hiring one more of
   the same is weak: they build, they do not buy.
3. COMMITTED TO A PROJECT THAT NEEDS IT (high). Announced a replatform, a rebuild, a migration, a new
   product or market launch, or a named system going live, where the client's work is obviously required.
4. NEWLY ABLE TO SPEND (medium). Funding, a grant, an acquisition, a new site: budget exists, but nothing
   says they want this particular work. Never give this very_high.
5. INFERRED PRESSURE (low). "They are probably struggling with X." This is a guess, not a signal. Propose
   at most one, and only if the levels above produce nothing.

At least two of your signals must come from levels 1 and 2. Write every signal as an observable public
event someone could link to, never as a state of mind or a condition you assume.

How to write level-1 queries, which is where most budget is wasted:
- Search for the document the BUYER wrote, not for pages ABOUT requests for proposals. Agencies publish
  "how to write an RFP" and "we respond to RFPs" pages full of the same words, so "request for proposal"
  plus "website redesign" mostly returns sellers. Prefer filetype:pdf or filetype:docx, plus ONE phrase only
  a buyer's brief contains: "submission deadline", "scope of work", "we invite proposals", "evaluation
  criteria", "proposals should be sent to", "the successful supplier".
- Keep queries broad enough to match: at most 3 quoted phrases per query (the subject plus one buyer
  phrase is usually right). Stacking four or five exact phrases matches nothing and wastes the search.
- Exclude seller vocabulary where it helps: -"our services" -"our clients" -"case study" -"we help".
- Google has no wildcard in site:. Write site:co.uk, never site:*.co.uk (that form returns nothing).

Before you propose a channel, ask whether THIS client could actually win work through it. A signal the
client cannot act on is worse than no signal: it fills the list with companies they can never sell to.
- Public-sector tenders and government procurement are only worth searching when the client is based in
  that country and shows evidence of public-sector work. An offshore or overseas supplier cannot
  realistically bid: those contracts need a local entity, supplier registration, prior public contracts
  and often security clearance. For an offshore delivery firm, searching them is wasted budget.
- An offshore or outsourcing supplier sells to private companies and to other agencies that subcontract.
  Its best signals are private demand: someone asking for a development or delivery partner, an agency
  looking to white-label, a company hiring contract or overflow help it clearly cannot staff in-house.
- A local supplier selling into its own market can use public procurement, local trade press and
  regional business news.
Say in `sells_how` which of these the client is, and let that decide the channels you search.
"""

# Every place a buyer can be found, with what it is good and bad at. The planner chooses from this, and
# says why; code then checks each choice (eligibility, URL rules) before anything is spent.
CHANNEL_CATALOG = """
WHERE BUYERS CAN BE FOUND. Choose the channels that fit THIS client and THIS ask, and explain the choice
in `where_to_look`. Web search is only one channel, and often not the best.

- signals (web and news search): pages a buyer wrote itself - its careers page, press release, blog, a
  dated news story about it. Best for funding, launches, expansions, hiring announcements.
- buyer documents (inside signals, filetype:pdf / filetype:docx): briefs and RFPs a PRIVATE company
  published. Pair one buyer-only phrase ("scope of work", "submission deadline") with the subject.
- community posts (inside signals, site: queries): owners asking for a supplier in public forums, e.g.
  site:reddit.com/r/smallbusiness "recommend an agency". Only forums readable without logging in.
- job_board_source (company job boards read through their APIs: Greenhouse, Lever, Ashby, SmartRecruiters,
  Workable, Recruitee, Teamtailor; the last three carry many small agencies): companies hiring for a skill.
  Strong when the client sells that skill as a service or white-label; useless when the buyer would never
  hire it. Set its category to the kind of company wanted ("digital agency"): searches then name it.
- job_ads_source (LinkedIn job ads in any country, plus Reed in the UK; recruiters excluded): companies
  advertising the skill themselves, dated, with the employer named. Signal-first: strongest when the client
  sells that skill as white-label or overflow capacity TO companies like the advertiser (an agency hiring
  developers has more client work than staff). Give 3-6 short searches a hiring company would title its ad
  with, e.g. "wordpress developer", "web developer agency", "AI automation developer".
- contract_awards_source (UK Contracts Finder award notices): SME suppliers that just WON public
  contracts. Not tenders to bid on: winners who now need capacity. Strong for subcontracting and
  white-label offers to the winners. UK buyers only.
- browse_sources (a browser agent reads ONE public page you name): lists that ARE buyers - an accelerator
  or incubator cohort, a trade-show exhibitor list, a 'fastest growing' list, a membership directory of
  the buyer's industry. Give the exact public URL you are confident exists and the goal (which companies
  to list). Never a login page, social network, job site or freelance marketplace. At most 2; each costs
  real money, so use one only when that page is a better list of buyers than a search can give.
- public tenders: ONLY for a client eligible to bid (local entity with public-sector work shown on its own
  site). Code removes tender searches for anyone else, so proposing them otherwise only wastes the plan.
"""

# Used when there is no LLM to tailor the plan. Explicit demand first, same as the ladder above.
GENERIC_SIGNALS = (
    (
        "asking_for_proposals",
        "Publicly invited proposals, quotes or agencies for this kind of work",
        "very_high",
        "search",
        (
            '"{target}" "request for proposal" {country}',
            '"{target}" "seeking quotes" OR "invitation to tender" {country}',
            '"{target}" "looking for an agency" OR "looking for a partner" {country}',
        ),
    ),
    (
        "hiring_the_skill",
        "Advertising for exactly this skill, especially contract or first-time hires",
        "high",
        "search",
        (
            '"{target}" "contract" OR "freelance" vacancy {country}',
            '"{target}" careers "we are looking for" {country}',
        ),
    ),
    (
        "project_committed",
        "Announced a rebuild, replatform, migration or launch that needs this work",
        "high",
        "news",
        ('"{target}" replatform OR rebuild OR migration {country}', '"{target}" launches new {country}'),
    ),
    (
        "newly_funded",
        "Raised money, so the budget exists even though nothing says they want this work",
        "medium",
        "news",
        ('"{target}" raises funding {country}',),
    ),
)


class DraftedICP(BaseModel):
    industries: list[str] = Field(description="Industries the ideal customer operates in")
    sub_industries: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(description="Countries, regions or cities")
    size_band: str = Field(description="Employee or revenue band, e.g. '10-200 staff'")
    business_model: str = Field(description="e.g. B2B SaaS, D2C brand, 3PL, manufacturer")
    why_they_buy: list[str] = Field(description="Plain-language reasons this kind of company buys the offer")
    disqualifiers: list[str] = Field(default_factory=list, description="What rules a company out")


class DraftedSignal(BaseModel):
    type: str = Field(description="Short snake_case label, e.g. hiring_warehouse_ops")
    description: str = Field(description="What this event means and why it implies a need now")
    strength: Literal["very_high", "high", "medium", "low"]
    kind: Literal["search", "news"] = "search"
    queries: list[str] = Field(description="2-4 Google queries that surface SPECIFIC companies showing it")


class DraftedSource(BaseModel):
    """A public dataset where every row is already a candidate buyer. Far higher yield than a web
    search, because the row is the company's own posting rather than a page about it."""

    category: str = Field(description="What these companies are, 2-4 words, e.g. 'UK ecommerce brand'")
    search_queries: list[str] = Field(
        description="2-4 Google queries that surface COMPANY JOB BOARDS, e.g. "
        'site:jobs.lever.co "contract developer" United Kingdom. We then read each board\'s '
        "public API directly, so these queries only have to find boards, not companies."
    )
    role_keywords: list[str] = Field(
        description="A posting counts if its title contains one of these as whole words, e.g. "
        "['developer', 'engineer', 'webmaster']"
    )
    locations: list[str] = Field(
        default_factory=list, description="Posting location must contain one of these; empty means anywhere"
    )


class DraftedAwardsSource(BaseModel):
    """UK Contracts Finder award notices: suppliers that just won public contracts."""

    keywords: list[str] = Field(
        description="Words in the contract title or description, e.g. ['website', 'software']"
    )
    cpv_prefixes: list[str] = Field(
        default_factory=list, description="CPV code prefixes, e.g. ['72'] for IT services"
    )
    why: str = Field(description="Why the winners of these contracts are buyers for this client")


class DraftedJobAdsSource(BaseModel):
    """Job ads with the employer named (LinkedIn; Reed in the UK): companies advertising the skill."""

    keywords: list[str] = Field(description="3-6 short job-ad searches, e.g. 'freelance web developer'")
    category: str = Field(
        default="", description="What the advertising companies should be, e.g. 'digital agency'"
    )
    why: str = Field(description="Why a company advertising these roles is a buyer for this client")


class DraftedBrowseSource(BaseModel):
    """One public page a browser agent reads, listing companies that are likely buyers."""

    url: str = Field(description="Exact public URL of a page that lists the companies")
    goal: str = Field(description="Which companies to list from the page, in plain words")
    why: str = Field(description="Why the companies on this page are buyers for this client")


class PlanOut(BaseModel):
    """The plan for one request. The client itself is described separately (ClientBriefOut), once."""

    offer: str = Field(description="One line: what the client sells, in the buyer's words")
    icp: DraftedICP
    signals: list[DraftedSignal] = Field(description="3-5 buying signals, strongest first")
    buyer_titles: list[str] = Field(description="Job titles that own this budget, most senior first")
    negative_signals: list[str] = Field(
        default_factory=list, description="Evidence a company is NOT a buyer, e.g. already has it in-house"
    )
    where_to_look: str = Field(
        default="",
        description="Two to four plain sentences for the operator: where this client's buyers can be found "
        "and why these channels, including what you deliberately left out and why.",
    )
    job_ads_source: DraftedJobAdsSource | None = Field(
        default=None,
        description="UK buyers only: job-ad searches for companies hiring contractors (see catalog)",
    )
    contract_awards_source: DraftedAwardsSource | None = Field(
        default=None,
        description="Only for UK buyers, when winners of public contracts need this client's help",
    )
    browse_sources: list[DraftedBrowseSource] = Field(
        default_factory=list, max_length=2, description="0-2 public pages that list buyers (see the catalog)"
    )
    job_board_source: DraftedSource | None = Field(
        default=None,
        description="Fill this in whenever companies hiring for a skill is a buying signal for this "
        "client. Leave null only when hiring is irrelevant to what they sell.",
    )


@dataclass
class Plan:
    """A drafted research plan, ready for review. `generated` is False for the fallback template."""

    ask: str
    count: int
    country: str
    client_name: str
    client_website: str | None
    client_summary: str
    offer: str
    icp: dict
    signals: list[dict]
    buyer_titles: list[str]
    sources: list[dict] = field(default_factory=list)  # structured datasets, searched before the web
    client_location: str = ""  # where the client sells from: decides which channels they can use at all
    sells_how: str = ""  # e.g. offshore delivery partner vs local supplier bidding for contracts
    negative: list[str] = field(default_factory=list)
    why_choose: list[str] = field(default_factory=list)
    freshness_days: int = 90
    personalized_email: bool = True
    include_icp_only: bool = False
    cost_cap_inr: float | None = None
    generated: bool = True
    pages_read: list[str] = field(default_factory=list)
    note: str = ""
    where_to_look: str = ""  # the planner's reasoning about channels, shown to the operator
    profile: ClientProfile = field(default_factory=ClientProfile)  # verified facts about the client
    exclusions: list[dict] = field(default_factory=list)  # [{what, why}] removed by eligibility rules

    def enforce_eligibility(self) -> "Plan":
        """Drops every search the client cannot act on, recording why. Idempotent: safe to run again
        after the operator edits the plan."""
        result = apply_eligibility(self.signals, self.profile, self.country, self.sources)
        self.signals, self.sources = result.signals, result.sources
        seen = {(e["what"], e["why"]) for e in self.exclusions}
        self.exclusions += [vars(e) for e in result.exclusions if (e.what, e.why) not in seen]
        return self

    def to_spec(self, client_key: str) -> JobSpec:
        return JobSpec(
            client=ClientSpec(key=client_key, name=self.client_name, website=self.client_website),
            ask=self.ask,
            countries=[self.country],
            count=self.count,
            freshness_days=self.freshness_days,
            personalized_email=self.personalized_email,
            include_icp_only=self.include_icp_only,
            cost_cap_inr=self.cost_cap_inr,
            icp=self.icp,
            # Most searches find no buyer, so the budget has to grow with the ask: ~8 per lead wanted.
            max_search_queries=min(300, max(30, self.count * 8)),
            playbook=Playbook(
                signals=[SignalSpec(**s) for s in self.signals],
                buyer_titles=self.buyer_titles,
                negative=self.negative,
            ),
            sources=[SourceSpec(**s) for s in self.sources],
            sender=SenderSpec(
                name=self.client_name,
                offer=self.offer,
                why_choose=self.why_choose,
                signature=self.client_name,
            )
            if self.personalized_email
            else None,
        )


async def read_client_site(website: str, fetcher: Fetcher) -> tuple[str, list[str]]:
    """The client's own pages, as text. Returns (text, urls actually read)."""
    domain = normalize_domain(website)
    if not domain:
        return "", []
    home = await fetcher.fetch(f"https://{domain}/")
    if not home.ok:
        home = await fetcher.fetch(f"https://www.{domain}/")
    if not home.ok:
        return "", []
    pages = {home.final_url: home.text}
    for url in pick_internal_links(home.html, home.final_url, domain)[:MAX_CLIENT_PAGES]:
        page = await fetcher.fetch(url)
        if page.ok and page.text:
            pages[page.final_url] = page.text
    text = "\n\n".join(f'<page url="{u}">\n{t[:MAX_CHARS_PER_PAGE]}\n</page>' for u, t in pages.items() if t)
    return text, list(pages)


class QueryRepair(BaseModel):
    original: str = Field(description="The flawed query, copied exactly")
    replacement: str = Field(description="A corrected query that keeps the same intent")


class QueryRepairOut(BaseModel):
    repairs: list[QueryRepair]


class StrongSignalsOut(BaseModel):
    signals: list[DraftedSignal] = Field(description="1-2 signals that show demand (very_high or high)")


_STRONG = ("very_high", "high")


async def _add_strong_signals(plan: Plan, llm) -> None:
    """A plan whose signals only show budget or guesses finds companies with no reason to buy now."""
    out = await llm.parse(
        llm.judge_model,
        f"This research plan has no signal showing demand. Propose 1-2 that do.\n{DEMAND_LADDER}\n"
        "Each query must return a specific named company, with at most 3 quoted phrases.",
        f"Client: {plan.client_summary}\nOffer: {plan.offer}\nAsk: {plan.ask}\n"
        f"Buyers in: {COUNTRIES.get(plan.country, plan.country)}\nICP: {plan.icp}",
        StrongSignalsOut,
        max_output_tokens=3000,
        effort="medium",
    )
    plan.signals += [sig.model_dump() for sig in out.signals if sig.strength in _STRONG and sig.queries]


_REPAIR_RULES = {
    "too_narrow": "requires more than 3 exact phrases, so it matches almost nothing: keep at most 3 quoted "
    "phrases a result must contain (OR-alternatives count as one; exclusions do not count)",
    "blocked_platform": "searches a job aggregator, social network or freelance marketplace, which we do not "
    "use: target the buyers' own pages instead",
    "site_wildcard": "uses site:*. which Google does not support: use site:co.uk style",
    "ineligible_tenders": "hunts public tenders, which this client cannot bid for: ask for private demand instead",
}


async def repair_plan(plan: Plan, llm) -> Plan:
    """Repairs what the quality checks find. Flawed queries get one model rewrite, then a deterministic
    last resort drops any still flawed (a signal keeps its good queries). A plan with no demand signal is
    asked for one. Never raises: a failed model call falls through to the deterministic step."""
    from app.services.plan_quality_service import query_issues

    if llm is not None and plan.signals and not any(s.get("strength") in _STRONG for s in plan.signals):
        try:
            await _add_strong_signals(plan, llm)
            plan.enforce_eligibility()
        except Exception as exc:
            logger.warning("adding a demand signal failed: %s: %s", type(exc).__name__, exc)
    issues = query_issues(plan)
    if not issues:
        return plan
    if llm is not None:
        listing = "\n".join(f"- {i.detail}\n  problem: {_REPAIR_RULES[i.check]}" for i in issues)
        try:
            out = await llm.parse(
                llm.extract_model,
                "Rewrite each flawed Google query so the problem goes away while it still finds the same kind "
                "of company. Return one replacement per query, copying the original exactly.",
                f"Client: {plan.client_summary}\nBuyers in: {COUNTRIES.get(plan.country, plan.country)}\n\n{listing}",
                QueryRepairOut,
                max_output_tokens=3000,
            )
            fixes = {r.original.strip(): r.replacement.strip() for r in out.repairs if r.replacement.strip()}
            plan.signals = [
                {**s, "queries": [fixes.get(q.strip(), q) for q in s.get("queries", [])]}
                for s in plan.signals
            ]
            plan.enforce_eligibility()
        except Exception as exc:
            logger.warning("plan repair failed: %s: %s", type(exc).__name__, exc)
    still_bad = {i.detail for i in query_issues(plan)}
    if still_bad:
        repaired = []
        for signal in plan.signals:
            good = [q for q in signal.get("queries", []) if q not in still_bad]
            if good:
                repaired.append({**signal, "queries": good})
        plan.signals = repaired
    return plan


def planned_sources(out: "PlanOut", country: str) -> list[dict]:
    """The structured sources the planner chose, as SourceSpec dicts. URL and country rules are applied here;
    eligibility runs after, on the whole plan."""
    sources: list[dict] = []
    if out.job_board_source:
        sources.append(out.job_board_source.model_dump() | {"type": "ats_jobs"})
    if out.job_ads_source and out.job_ads_source.keywords:  # LinkedIn anywhere; Reed added in the UK
        ads = out.job_ads_source
        sources.append(
            {
                "type": "job_ads",
                "keywords": ads.keywords[:6],
                "category": ads.category or None,
                "why": ads.why,
            }
        )
    if out.contract_awards_source and country == "GB":  # Contracts Finder is a UK dataset
        awards = out.contract_awards_source
        sources.append(
            {
                "type": "contracts_awards",
                "keywords": awards.keywords,
                "cpv_prefixes": awards.cpv_prefixes,
                "why": awards.why,
            }
        )
    for browse in out.browse_sources[:2]:
        sources.append(
            {"type": "agent_browse", "url": browse.url.strip(), "goal": browse.goal, "why": browse.why}
        )
    return sources


def fallback_plan(
    ask: str,
    count: int,
    country: str,
    website: str | None,
    client_name: str,
    reason: str,
    site_text: str = "",
) -> Plan:
    """A generic plan, labelled as such. Honest default: it is a starting point to edit, not research."""
    where = COUNTRIES.get(country, country)
    target = ask.strip() or "company"
    signals = [
        {
            "type": t,
            "description": d,
            "strength": s,
            "kind": k,
            "queries": [q.format(target=target, country=where) for q in qs],
        }
        for t, d, s, k, qs in GENERIC_SIGNALS
    ]
    return Plan(
        ask=ask,
        count=count,
        country=country,
        client_name=client_name,
        client_website=website,
        client_summary="Not researched: the plan below is a generic template.",
        offer=ask,
        icp={
            "industries": [],
            "geographies": [where],
            "size_band": "",
            "business_model": "",
            "why_they_buy": [],
            "disqualifiers": [],
        },
        signals=signals,
        buyer_titles=["founder", "managing director", "head of operations", "marketing director"],
        negative=[],
        why_choose=[],
        generated=False,
        note=f"Generic template ({reason}). Edit the ICP and signals before starting, or the run will "
        "research the wrong companies.",
        profile=verify_client_facts(ClientFactsOut(), site_text, website),
    ).enforce_eligibility()


class ClientBriefOut(BaseModel):
    """What the client is, read once from its own website."""

    client_facts: ClientFactsOut = Field(default_factory=ClientFactsOut)
    client_summary: str = Field(description="One sentence: what the client does, from their own site")
    client_location: str = Field(default="", description="City and country it sells from, if the site says")
    sells_how: str = Field(
        default="",
        description="How it realistically wins work: e.g. 'offshore delivery partner for overseas agencies', "
        "'local supplier bidding for public contracts', 'inbound SaaS'",
    )
    offer: str = Field(description="One line: what it sells, in a buyer's words")
    why_choose: list[str] = Field(
        default_factory=list, description="Checkable reasons to pick it, only as stated on its own site"
    )


@dataclass
class ClientBrief:
    """Verified, reusable knowledge about the client. Empty when there is no readable website."""

    summary: str = ""
    location: str = ""
    sells_how: str = ""
    offer: str = ""
    why_choose: list[str] = field(default_factory=list)
    profile: ClientProfile = field(default_factory=ClientProfile)
    pages_read: list[str] = field(default_factory=list)
    from_cache: bool = False
    note: str = ""

    def as_text(self, target_country: str) -> str:
        """The short form the planner reads instead of the whole website (~300 tokens, not ~7,000)."""
        if not self.summary:
            return "Nothing is known about the client beyond the request: plan from the request alone."
        country = self.profile.country
        based = (
            f"{COUNTRIES.get(country, country)} ({self.profile.country_evidence})"
            if country
            else "not stated"
        )
        public = (
            f"yes: {self.profile.public_sector_evidence}"
            if self.profile.public_sector_evidence
            else "none shown"
        )
        lines = [
            f"What it does: {self.summary}",
            f"Based in: {based}",
            f"Wins work as: {self.sells_how or '-'}",
            f"Sells: {self.offer or '-'}",
            f"Public-sector work shown on its site: {public}",
            f"Buyers wanted in: {COUNTRIES.get(target_country, target_country)}",
        ]
        return "\n".join(lines)


async def learn_client(
    website: str | None, client_key: str | None, llm, fetcher: Fetcher, use_cache: bool = True
) -> tuple[ClientBrief, str]:
    """(brief, site text read). The website is read and understood ONCE per client: the result is saved
    and reused by later runs until it is 30 days old. With no website, or no model, the brief is empty
    and the plan works from the request alone (safely: no tenders, no claims about the client)."""
    if not website:
        return ClientBrief(note="No client website was given, so the plan is based on the request alone."), ""
    if (
        use_cache
        and client_key
        and (saved := await asyncio.to_thread(client_repository.get_fresh, client_key, website))
    ):
        return ClientBrief(
            saved.summary,
            saved.location,
            saved.sells_how,
            saved.offer,
            list(saved.why_choose),
            ClientProfile(**(saved.facts or {})),
            list(saved.pages_read),
            from_cache=True,
        ), ""
    site_text, pages = "", []
    try:
        site_text, pages = await read_client_site(website, fetcher)
    except Exception as exc:  # a client site being down must not block planning
        logger.warning("could not read client site %s: %s", website, type(exc).__name__)
    hints_only = ClientBrief(
        profile=verify_client_facts(ClientFactsOut(), site_text, website), pages_read=pages
    )
    if not site_text:
        hints_only.note = "The client's website could not be read, so the plan is based on the request alone."
        return hints_only, site_text
    if llm is None:
        return hints_only, site_text
    out = await llm.parse(
        llm.extract_model,
        "Describe this company from its own website only. Never invent facts, clients, results or numbers; "
        "leave a field empty when the site does not say. Quote the site's exact words where asked.",
        site_text,
        ClientBriefOut,
        max_output_tokens=2000,
    )
    brief = ClientBrief(
        out.client_summary,
        out.client_location,
        out.sells_how,
        out.offer,
        out.why_choose,
        verify_client_facts(out.client_facts, site_text, website),
        pages,
    )
    if client_key:
        await asyncio.to_thread(
            client_repository.save,
            client_key,
            website,
            summary=brief.summary,
            location=brief.location,
            sells_how=brief.sells_how,
            offer=brief.offer,
            why_choose=brief.why_choose,
            facts=brief.profile.model_dump(),
            pages_read=brief.pages_read,
        )
    return brief, site_text


async def draft_plan(
    website: str | None,
    ask: str,
    count: int,
    country: str,
    llm,
    fetcher: Fetcher,
    client_name: str = "",
    client_key: str | None = None,
    use_cache: bool = True,
) -> Plan:
    """Drafts the plan for this request. Never raises: a failure returns the labelled fallback.

    Two model calls the first time a client is seen (understand the client, then plan the request), one
    every time after: the client part is saved and the planner reads its short summary, not the site."""
    name = client_name.strip() or (normalize_domain(website or "") or "Client")
    try:
        brief, site_text = await learn_client(website, client_key, llm, fetcher, use_cache)
    except Exception as exc:
        logger.warning("understanding the client failed: %s: %s", type(exc).__name__, exc)
        brief, site_text = ClientBrief(note="We could not understand the client's website just now."), ""
    if llm is None:
        plan = fallback_plan(ask, count, country, website, name, "no LLM configured", site_text)
        plan.profile = brief.profile
        return plan.enforce_eligibility()
    data = (
        f"The client's request: {ask}\nHow many companies they want: {count}\n"
        f"Country the BUYERS should be in: {COUNTRIES.get(country, country)}\n\n"
        f"About the client (verified from its own website):\n{brief.as_text(country)}\n"
        "Only propose channels a company in this position can actually sell through."
    )
    try:
        out = await llm.parse(
            llm.judge_model,
            "You plan B2B lead research. From what we know about the client and their request, write the "
            "ideal customer profile and the signal playbook the research will run on.\n"
            f"{DEMAND_LADDER}\n{CHANNEL_CATALOG}\n"
            "Name the work in the words a buyer would use when asking for it, not the words the client "
            "uses to sell it: someone wanting what this client sells will write 'need a developer for', "
            "'looking for an agency to', 'RFP for', not the client's own marketing language.\n"
            "Each query must be able to return a specific named company. Favour the phrasing of a real "
            "post: quoted asks, the company's own careers and news pages, and trade or regional press. "
            "Public tender portals only if the client is eligible to bid (see above): a local entity "
            "with public-sector work on its own site. Use the target country's own wording and outlets.\n"
            "Never write a query against LinkedIn, Indeed, Glassdoor, Reed, Totaljobs or any other job "
            "aggregator or business directory: we do not scrape them, and a result there names the board, "
            "not a company we can research.\n"
            "THE TRAP TO AVOID: searching the words of the demand returns the people who SELL it, not the "
            "people who want it. 'RFP for web development' returns agencies describing how they answer "
            "RFPs. Every query must be one that only a buyer's own page would match: their careers page, "
            "their press release, their procurement page, their job posting. Add negative terms that cut "
            'out suppliers, e.g. -"our services" -"we provide" -"get a quote" -"portfolio", and '
            "pair the demand with who the buyer IS (their industry, their size) so the result is a "
            "company in the ICP rather than anyone at all.\n"
            "Put ATS board queries (greenhouse, lever, ashby, smartrecruiters) in job_board_source, never "
            "in a signal: we read those boards through their own APIs, where a posting is dated, "
            "first-party and quotable. A signal query that returns a board page is wasted.\n"
            "'why_choose' may contain only claims stated on the client's own site; leave it empty if the "
            "site was not readable. Never invent clients, results or numbers.",
            data,
            PlanOut,
            effort="medium",
            max_output_tokens=8000,
        )
    except Exception as exc:
        logger.warning("ICP drafting failed: %s: %s", type(exc).__name__, exc)
        plan = fallback_plan(
            ask, count, country, website, name, f"drafting failed: {type(exc).__name__}", site_text
        )
        plan.profile = brief.profile
        return plan.enforce_eligibility()
    order = {"very_high": 0, "high": 1, "medium": 2, "low": 3}
    signals = sorted((s.model_dump() for s in out.signals), key=lambda s: order.get(s["strength"], 9))
    plan = Plan(
        ask=ask,
        count=count,
        country=country,
        client_name=name,
        client_website=website,
        client_summary=brief.summary,
        client_location=brief.location,
        sells_how=brief.sells_how,
        offer=out.offer or brief.offer,
        icp=out.icp.model_dump(),
        signals=signals,
        buyer_titles=out.buyer_titles,
        sources=planned_sources(out, country),
        where_to_look=out.where_to_look,
        negative=out.negative_signals,
        why_choose=brief.why_choose,
        pages_read=brief.pages_read,
        note=brief.note,
        profile=brief.profile,
    ).enforce_eligibility()
    return await repair_plan(plan, llm)


# --- storing a plan on the job row, between review and launch -----------------------------------


def plan_to_json(plan: Plan) -> dict:
    """What goes in `jobs.icp`. Keeps the shape the pipeline already reads (`icp` + `playbook`) and adds
    the intake context, so a run can be rebuilt from its own row."""
    return {
        "icp": plan.icp,
        "playbook": {"signals": plan.signals, "buyer_titles": plan.buyer_titles, "negative": plan.negative},
        "intake": {
            "client_summary": plan.client_summary,
            "client_location": plan.client_location,
            "sells_how": plan.sells_how,
            "offer": plan.offer,
            "why_choose": plan.why_choose,
            "generated": plan.generated,
            "pages_read": plan.pages_read,
            "note": plan.note,
            "sources": plan.sources,
            "include_icp_only": plan.include_icp_only,
            "profile": plan.profile.model_dump(),
            "where_to_look": plan.where_to_look,
            "exclusions": plan.exclusions,
        },
    }


def plan_from_job(job) -> Plan:
    stored = job.icp or {}
    intake = stored.get("intake") or {}
    playbook = stored.get("playbook") or {}
    return Plan(
        ask=job.ask,
        count=job.requested_count,
        country=(job.target_countries or ["GB"])[0],
        client_name=job.client_name,
        client_website=job.client_website,
        client_summary=intake.get("client_summary", ""),
        client_location=intake.get("client_location", ""),
        sells_how=intake.get("sells_how", ""),
        offer=intake.get("offer", job.ask),
        icp=stored.get("icp") or {},
        signals=playbook.get("signals") or [],
        buyer_titles=playbook.get("buyer_titles") or [],
        negative=playbook.get("negative") or [],
        why_choose=intake.get("why_choose") or [],
        sources=intake.get("sources") or [],
        freshness_days=job.freshness_days,
        personalized_email=job.personalized_email,
        include_icp_only=bool(intake.get("include_icp_only")),
        cost_cap_inr=job.cost_cap_inr,
        generated=bool(intake.get("generated", True)),
        pages_read=intake.get("pages_read") or [],
        note=intake.get("note", ""),
        profile=ClientProfile(**(intake.get("profile") or {})),
        exclusions=intake.get("exclusions") or [],
        where_to_look=intake.get("where_to_look", ""),
    )


# --- the two operator forms -----------------------------------------------------------------------


def validate_intake_form(
    website: str,
    ask: str,
    count: int,
    country: str,
    freshness_days: int,
    cost_cap_inr: float,
    client_name: str = "",
) -> dict[str, str]:
    """Field -> message for everything wrong with a new-run request; empty when it can be drafted."""
    errors: dict[str, str] = {}
    if website.strip() and not normalize_domain(website):
        errors["website"] = (
            "That doesn't look like a website. Use something like acme.co.uk, or leave it empty."
        )
    if len(ask.strip()) < 10:
        errors["ask"] = "Describe who to find, e.g. 'UK 3PLs hiring warehouse staff'."
    if not 1 <= count <= 500:
        errors["count"] = "Between 1 and 500."
    if not 1 <= freshness_days <= 365:
        errors["freshness_days"] = "Between 1 and 365 days."
    if not 0 < cost_cap_inr <= MAX_COST_CAP_INR:
        errors["cost_cap_inr"] = f"Between ₹1 and ₹{MAX_COST_CAP_INR:,.0f}."
    if country.upper() not in COUNTRIES:
        errors["country"] = "Pick a country from the list."
    if not website.strip() and not client_name.strip():
        errors["website"] = (
            "Give the client's website (best), or at least the client's name under More options."
        )
    return errors


def _lines(value: str) -> list[str]:
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def _commas(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


@dataclass
class ReviewedPlanForm:
    """The review page's form, as submitted. Signal fields are parallel lists, one entry per signal."""

    ask: str = ""
    count: int = 25
    freshness_days: int = 90
    cost_cap_inr: float = 0.0
    personalized_email: bool = False
    include_icp_only: bool = False
    industries: str = ""
    geographies: str = ""
    size_band: str = ""
    business_model: str = ""
    why_they_buy: str = ""
    disqualifiers: str = ""
    buyer_titles: str = ""
    negative: str = ""
    offer: str = ""
    why_choose: str = ""
    signal_type: list[str] = field(default_factory=list)
    signal_description: list[str] = field(default_factory=list)
    signal_strength: list[str] = field(default_factory=list)
    signal_kind: list[str] = field(default_factory=list)
    signal_queries: list[str] = field(default_factory=list)

    def signals(self) -> list[dict]:
        """A cleared name drops a signal; a signal with no search query cannot be searched for."""
        out = []
        for i, name in enumerate(self.signal_type):
            queries = _lines(_at(self.signal_queries, i))
            if not name.strip() or not queries:
                continue
            strength = _at(self.signal_strength, i)
            out.append(
                {
                    "type": name.strip(),
                    "description": _at(self.signal_description, i) or name.strip(),
                    "strength": strength if strength in SIGNAL_STRENGTHS else "medium",
                    "kind": "news" if _at(self.signal_kind, i) == "news" else "search",
                    "queries": queries,
                }
            )
        return out


def _at(values: list[str], i: int) -> str:
    return values[i].strip() if i < len(values) else ""


def build_reviewed_spec(job, form: ReviewedPlanForm) -> JobSpec:
    """The job spec for a reviewed plan, as edited. Raises BadRequestError when it cannot run."""
    intake = (job.icp or {}).get("intake", {})
    plan = Plan(
        ask=form.ask.strip() or job.ask,
        count=form.count,
        country=(job.target_countries or ["GB"])[0],
        client_name=job.client_name,
        client_website=job.client_website,
        client_summary=intake.get("client_summary", ""),
        offer=form.offer.strip() or form.ask.strip(),
        icp={
            "industries": _commas(form.industries),
            "geographies": _commas(form.geographies),
            "size_band": form.size_band.strip(),
            "business_model": form.business_model.strip(),
            "why_they_buy": _lines(form.why_they_buy),
            "disqualifiers": _lines(form.disqualifiers),
        },
        signals=form.signals(),
        buyer_titles=_commas(form.buyer_titles),
        negative=_lines(form.negative),
        sources=intake.get("sources") or [],
        why_choose=_lines(form.why_choose),
        freshness_days=form.freshness_days,
        personalized_email=form.personalized_email,
        include_icp_only=form.include_icp_only,
        cost_cap_inr=form.cost_cap_inr or job.cost_cap_inr,
        profile=ClientProfile(**(intake.get("profile") or {})),
    ).enforce_eligibility()
    try:
        spec = plan.to_spec(job.client_key)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise BadRequestError(f"The plan is not runnable: {first['loc'][-1]} — {first['msg']}") from exc
    if not spec.playbook.signals:
        raise BadRequestError("Keep at least one signal with a search query.")
    if not spec.playbook.buyer_titles:
        raise BadRequestError("Name at least one buyer title to look for.")
    return spec
