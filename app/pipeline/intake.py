"""Step 0: turn a plain request into a research plan.

The operator gives three things: the client's website, who they want to reach, and how many. This module
reads the client's own site, then drafts the ICP and the signal playbook that the rest of the pipeline
runs on. The draft is always shown for review before anything is spent, because a wrong playbook is the
one error the downstream quote, date and email checks cannot catch.

With no LLM available it falls back to a generic, clearly-labelled template built from the request, so
the product still works and never pretends a template was researched.
"""
import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app.core.fetcher import Fetcher
from app.core.normalize import normalize_domain
from app.pipeline.jobspec import ClientSpec, JobSpec, Playbook, SenderSpec, SignalSpec, SourceSpec
from app.pipeline.research import pick_internal_links

log = logging.getLogger("leadpro.intake")

MAX_CLIENT_PAGES = 4
MAX_CHARS_PER_PAGE = 6000

COUNTRIES = {"GB": "United Kingdom", "US": "United States", "IN": "India"}

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

# Used when there is no LLM to tailor the plan. Explicit demand first, same as the ladder above.
GENERIC_SIGNALS = (
    ("asking_for_proposals", "Publicly invited proposals, quotes or agencies for this kind of work",
     "very_high", "search",
     ('"{target}" "request for proposal" {country}', '"{target}" "seeking quotes" OR "invitation to tender" {country}',
      '"{target}" "looking for an agency" OR "looking for a partner" {country}')),
    ("hiring_the_skill", "Advertising for exactly this skill, especially contract or first-time hires",
     "high", "search",
     ('"{target}" "contract" OR "freelance" vacancy {country}', '"{target}" careers "we are looking for" {country}')),
    ("project_committed", "Announced a rebuild, replatform, migration or launch that needs this work",
     "high", "news",
     ('"{target}" replatform OR rebuild OR migration {country}', '"{target}" launches new {country}')),
    ("newly_funded", "Raised money, so the budget exists even though nothing says they want this work",
     "medium", "news",
     ('"{target}" raises funding {country}',)),
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
                    "site:jobs.lever.co \"contract developer\" United Kingdom. We then read each board's "
                    "public API directly, so these queries only have to find boards, not companies.")
    role_keywords: list[str] = Field(
        description="A posting counts if its title contains one of these as whole words, e.g. "
                    "['developer', 'engineer', 'webmaster']")
    locations: list[str] = Field(default_factory=list,
                                 description="Posting location must contain one of these; empty means anywhere")


class PlanOut(BaseModel):
    client_summary: str = Field(description="One sentence: what the client does, from their own site")
    client_location: str = Field(
        default="",
        description="Where the client is based and sells from, from their own site (city and country). "
                    "Empty if the site does not say.")
    sells_how: str = Field(
        default="",
        description="How this client realistically wins work: e.g. 'offshore delivery partner for "
                    "overseas agencies', 'local supplier bidding for public contracts', 'inbound SaaS'. "
                    "This decides which channels are worth searching at all.")
    offer: str = Field(description="One line: what the client sells, in the buyer's words")
    icp: DraftedICP
    signals: list[DraftedSignal] = Field(description="3-5 buying signals, strongest first")
    buyer_titles: list[str] = Field(description="Job titles that own this budget, most senior first")
    negative_signals: list[str] = Field(default_factory=list,
                                        description="Evidence a company is NOT a buyer, e.g. already has it in-house")
    why_choose: list[str] = Field(default_factory=list,
                                  description="Checkable reasons to pick this client, taken only from their own site")
    job_board_source: DraftedSource | None = Field(
        default=None,
        description="Fill this in whenever companies hiring for a skill is a buying signal for this "
                    "client. Leave null only when hiring is irrelevant to what they sell.")


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
    sells_how: str = ""        # e.g. offshore delivery partner vs local supplier bidding for contracts
    negative: list[str] = field(default_factory=list)
    why_choose: list[str] = field(default_factory=list)
    freshness_days: int = 90
    personalized_email: bool = True
    include_icp_only: bool = False
    cost_cap_inr: float | None = None
    generated: bool = True
    pages_read: list[str] = field(default_factory=list)
    note: str = ""

    def to_spec(self, client_key: str) -> JobSpec:
        return JobSpec(
            client=ClientSpec(key=client_key, name=self.client_name, website=self.client_website),
            ask=self.ask, countries=[self.country], count=self.count, freshness_days=self.freshness_days,
            personalized_email=self.personalized_email, include_icp_only=self.include_icp_only,
            cost_cap_inr=self.cost_cap_inr, icp=self.icp,
            # Most searches find no buyer, so the budget has to grow with the ask: ~8 per lead wanted.
            max_search_queries=min(300, max(30, self.count * 8)),
            playbook=Playbook(signals=[SignalSpec(**s) for s in self.signals],
                              buyer_titles=self.buyer_titles, negative=self.negative),
            sources=[SourceSpec(**s) for s in self.sources],
            sender=SenderSpec(name=self.client_name, offer=self.offer, why_choose=self.why_choose,
                              signature=self.client_name) if self.personalized_email else None,
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
    text = "\n\n".join(f"<page url=\"{u}\">\n{t[:MAX_CHARS_PER_PAGE]}\n</page>" for u, t in pages.items() if t)
    return text, list(pages)


def fallback_plan(ask: str, count: int, country: str, website: str | None, client_name: str,
                  reason: str) -> Plan:
    """A generic plan, labelled as such. Honest default: it is a starting point to edit, not research."""
    where = COUNTRIES.get(country, country)
    target = ask.strip() or "company"
    signals = [{"type": t, "description": d, "strength": s, "kind": k,
                "queries": [q.format(target=target, country=where) for q in qs]}
               for t, d, s, k, qs in GENERIC_SIGNALS]
    return Plan(
        ask=ask, count=count, country=country, client_name=client_name, client_website=website,
        client_summary="Not researched: the plan below is a generic template.",
        offer=ask, icp={"industries": [], "geographies": [where], "size_band": "", "business_model": "",
                        "why_they_buy": [], "disqualifiers": []},
        signals=signals, buyer_titles=["founder", "managing director", "head of operations", "marketing director"],
        negative=[], why_choose=[], generated=False,
        note=f"Generic template ({reason}). Edit the ICP and signals before starting, or the run will "
             "research the wrong companies.")


async def draft_plan(website: str | None, ask: str, count: int, country: str, llm, fetcher: Fetcher,
                     client_name: str = "") -> Plan:
    """Reads the client's site and drafts the plan. Never raises: a failure returns the labelled fallback."""
    name = client_name.strip() or (normalize_domain(website or "") or "Client")
    site_text, pages = ("", [])
    if website:
        try:
            site_text, pages = await read_client_site(website, fetcher)
        except Exception as exc:  # a client site being down must not block planning
            log.warning("could not read client site %s: %s", website, type(exc).__name__)
    if llm is None:
        return fallback_plan(ask, count, country, website, name, "no LLM configured")
    data = (f"The client's request: {ask}\nHow many companies they want: {count}\n"
            f"Country the BUYERS should be in: {COUNTRIES.get(country, country)}\n"
            "(The client may well be based somewhere else; work out where from their site, and only "
            "propose channels a company in that position can actually sell through.)\n\n"
            f"The client's own website{' (not readable)' if not site_text else ''}:\n{site_text or '(none)'}")
    try:
        out = await llm.parse(
            llm.judge_model,
            "You plan B2B lead research. From the client's own website and their request, write the ideal "
            "customer profile and the signal playbook the research will run on.\n"
            f"{DEMAND_LADDER}\n"
            "Name the work in the words a buyer would use when asking for it, not the words the client "
            "uses to sell it: someone wanting what this client sells will write 'need a developer for', "
            "'looking for an agency to', 'RFP for', not the client's own marketing language.\n"
            "Each query must be able to return a specific named company. Favour the phrasing of a real "
            "post: quoted asks, tender and procurement portals (for the UK, find-tender.service.gov.uk "
            "and contractsfinder.service.gov.uk), the company's own careers and news pages, and trade or "
            "regional press. Use the target country's own wording and outlets.\n"
            "Never write a query against LinkedIn, Indeed, Glassdoor, Reed, Totaljobs or any other job "
            "aggregator or business directory: we do not scrape them, and a result there names the board, "
            "not a company we can research.\n"
            "THE TRAP TO AVOID: searching the words of the demand returns the people who SELL it, not the "
            "people who want it. 'RFP for web development' returns agencies describing how they answer "
            "RFPs. Every query must be one that only a buyer's own page would match: their careers page, "
            "their press release, their procurement page, their job posting. Add negative terms that cut "
            "out suppliers, e.g. -\"our services\" -\"we provide\" -\"get a quote\" -\"portfolio\", and "
            "pair the demand with who the buyer IS (their industry, their size) so the result is a "
            "company in the ICP rather than anyone at all.\n"
            "Put ATS board queries (greenhouse, lever, ashby, smartrecruiters) in job_board_source, never "
            "in a signal: we read those boards through their own APIs, where a posting is dated, "
            "first-party and quotable. A signal query that returns a board page is wasted.\n"
            "'why_choose' may contain only claims stated on the client's own site; leave it empty if the "
            "site was not readable. Never invent clients, results or numbers.",
            data, PlanOut, effort="medium")
    except Exception as exc:
        log.warning("ICP drafting failed: %s: %s", type(exc).__name__, exc)
        return fallback_plan(ask, count, country, website, name, f"drafting failed: {type(exc).__name__}")
    order = {"very_high": 0, "high": 1, "medium": 2, "low": 3}
    signals = sorted((s.model_dump() for s in out.signals), key=lambda s: order.get(s["strength"], 9))
    return Plan(
        ask=ask, count=count, country=country, client_name=name, client_website=website,
        client_summary=out.client_summary, client_location=out.client_location,
        sells_how=out.sells_how, offer=out.offer, icp=out.icp.model_dump(),
        signals=signals, buyer_titles=out.buyer_titles,
        sources=[out.job_board_source.model_dump() | {"type": "ats_jobs"}] if out.job_board_source else [],
        negative=out.negative_signals, why_choose=out.why_choose, pages_read=pages,
        note="" if site_text else "The client's website could not be read, so the plan is based on the "
                                  "request alone. Check the ICP carefully.")


# --- storing a plan on the job row, between review and launch -----------------------------------

def plan_to_json(plan: Plan) -> dict:
    """What goes in `jobs.icp`. Keeps the shape the pipeline already reads (`icp` + `playbook`) and adds
    the intake context, so a run can be rebuilt from its own row."""
    return {
        "icp": plan.icp,
        "playbook": {"signals": plan.signals, "buyer_titles": plan.buyer_titles, "negative": plan.negative},
        "intake": {"client_summary": plan.client_summary, "client_location": plan.client_location,
                   "sells_how": plan.sells_how, "offer": plan.offer, "why_choose": plan.why_choose,
                   "generated": plan.generated, "pages_read": plan.pages_read, "note": plan.note,
                   "sources": plan.sources,
                   "include_icp_only": plan.include_icp_only},
    }


def plan_from_job(job) -> Plan:
    stored = job.icp or {}
    intake = stored.get("intake") or {}
    playbook = stored.get("playbook") or {}
    return Plan(
        ask=job.ask, count=job.requested_count, country=(job.target_countries or ["GB"])[0],
        client_name=job.client_name, client_website=job.client_website,
        client_summary=intake.get("client_summary", ""), client_location=intake.get("client_location", ""),
        sells_how=intake.get("sells_how", ""), offer=intake.get("offer", job.ask),
        icp=stored.get("icp") or {}, signals=playbook.get("signals") or [],
        buyer_titles=playbook.get("buyer_titles") or [], negative=playbook.get("negative") or [],
        why_choose=intake.get("why_choose") or [], sources=intake.get("sources") or [],
        freshness_days=job.freshness_days,
        personalized_email=job.personalized_email, include_icp_only=bool(intake.get("include_icp_only")),
        cost_cap_inr=job.cost_cap_inr, generated=bool(intake.get("generated", True)),
        pages_read=intake.get("pages_read") or [], note=intake.get("note", ""),
    )
