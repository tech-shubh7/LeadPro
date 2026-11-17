"""Quality checks on a research plan, run on every drafted plan and by the planner evals.

Each check names one concrete way a plan wastes money or misleads: a channel the client cannot sell
through, a search that cannot work, a missing piece the run needs. Problems that code can fix silently
are fixed elsewhere (eligibility, query cleaning); these are the ones a person should see.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.services.eligibility_service import PROCUREMENT, can_bid_public_tenders

if TYPE_CHECKING:  # the intake service imports this module: type-only import avoids a cycle
    from app.services.intake_service import Plan

# Searches against these return the platform, not a company we can research, or break their terms.
BLOCKED_IN_QUERIES = re.compile(
    r"\b(?:linkedin|indeed|glassdoor|reed\.co\.uk|totaljobs|ziprecruiter|monster|upwork|fiverr|freelancer\.com)\b",
    re.I,
)
MAX_QUOTED_PHRASES = 3  # more exact phrases than this match almost nothing (job 15)


_TERM = re.compile(r'(-?)"[^"]*"|\bOR\b|\S+')


def required_phrases(query: str) -> int:
    """How many exact phrases a result must contain. An excluded phrase (-"...") narrows nothing for
    matching purposes, and phrases joined by OR are one requirement, not several."""
    groups, previous_or = 0, False
    for match in _TERM.finditer(query):
        term = match.group(0)
        if term == "OR":
            previous_or = True
            continue
        if term.startswith('"') and not previous_or:
            groups += 1
        previous_or = False
    return groups


@dataclass(frozen=True)
class PlanIssue:
    check: str
    detail: str


def check_plan(plan: "Plan") -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    queries = [q for s in plan.signals for q in s.get("queries", [])]
    tenders_allowed, _ = can_bid_public_tenders(plan.profile, plan.country)
    if not tenders_allowed:
        issues += [PlanIssue("ineligible_tenders", q) for q in queries if PROCUREMENT.search(q)]
    issues += [PlanIssue("blocked_platform", q) for q in queries if BLOCKED_IN_QUERIES.search(q)]
    issues += [PlanIssue("site_wildcard", q) for q in queries if re.search(r"\bsite:\*", q)]
    issues += [PlanIssue("too_narrow", q) for q in queries if required_phrases(q) > MAX_QUOTED_PHRASES]
    if not plan.signals:
        issues.append(PlanIssue("no_signals", "the plan has no searchable signal"))
    if not plan.buyer_titles:
        issues.append(PlanIssue("no_buyer_titles", "nobody to contact"))
    if plan.generated and not plan.where_to_look.strip():
        issues.append(PlanIssue("no_reasoning", "where_to_look is empty"))
    strong = [s for s in plan.signals if s.get("strength") in ("very_high", "high")]
    if plan.signals and not strong:
        issues.append(PlanIssue("weak_signals_only", "no signal shows demand, only budget or guesses"))
    return issues


# Issues tied to one search query: these can be repaired query by query.
QUERY_CHECKS = frozenset({"ineligible_tenders", "blocked_platform", "site_wildcard", "too_narrow"})


def query_issues(plan: "Plan") -> list[PlanIssue]:
    return [i for i in check_plan(plan) if i.check in QUERY_CHECKS]
