# LeadPro research engine: how it finds great companies

The engine is not a fixed workflow. Each stage spends money only after a cheaper stage has said that a company is worth it, and every claim it outputs is verified by code.

## Built (2026-10-30)

| # | Technique | Where | Why it matters |
|---|---|---|---|
| 1 | **Agentic query planning with feedback.** Playbook queries, then the judge model writes new queries after seeing each query's yield (target companies vs recruiters, job boards, publishers, providers). Up to 3 rounds within `max_search_queries`. | `pipeline/discover.py` (`plan_queries`) | Searches improve during the run instead of repeating weak patterns |
| 2 | **Batch triage before any spend.** One cheap-model call classifies every search hit: which company it is about, entity type, own site or not, ICP fit 0-10, visible signal. Names must literally appear in the hit. | `discover.py` (`triage`) | Job boards, recruiters, publishers and 3PLs are filtered out before fetching or deep research |
| 3 | **Prior-ranked research with early stop.** Candidates are researched in order of triage fit; the run stops when N leads qualify. | `discover.py`, `runner.py` | Budget goes to the most promising companies first |
| 4 | **Multi-source evidence per company.** The company's own pages, a targeted news search (`"Company"`, last year), the source articles, and the registry (Companies House). | `pipeline/research.py` | Dated signals often live in third-party news, not on the company's site |
| 5 | **No good lead lost.** Sites that refuse automated access are researched from news, registry and search results (never bypassed) and flagged in `website_access`. Leads without a verified email go to "Good leads - no contact". | `research.py`, `export.py` | Keeps strong prospects while staying compliant |
| 6 | **Code-verified claims and trusted dates.** Quotes are checked against fetched text; dates only come from article metadata, URL dates, search-result dates or job dates. A quote-verified event date stated near the signal ("in March 2025") overrides a newer publication date, never the reverse; republished reports of one event are collapsed to the earliest. | `core/evidence_verify.py`, `core/dates.py` | The model cannot invent facts or freshness |
| 7 | **Spend reuse.** Serper results are cached in dev; verified emails are reused for 60 days (`EMAIL_REUSE_MAX_DAYS`); manual-mode answers are cached by request hash. | `serper.py`, `store.py`, `manual_provider.py` | Free credits stretch across reruns |
| 9 | **Identity by registry number.** A company number printed on the company's own site resolves a second domain (acme.com vs acme.co.uk) to the same company record, so it is neither researched twice in a run nor re-delivered to a client. Name-only registry matches never merge records. | `runner.py` (`RunGuards`), `store.py` (`resolve_company`) | No duplicates across domains |
| 10 | **Judge sees the playbook.** Each signal type's meaning and strength (`very_high`…`low`) is given to the judge, which is told that an event is not automatically a buying need. | `score.py`, job YAML | Fewer false "intent" scores |
| 8 | **Yield logging.** Every discovery query's hits, targets and non-target types are stored in `jobs.progress.discovery_queries`. | `runner.py` | Basis for learning across jobs (below) |

## Ready to switch on once the OpenAI key arrives (design, not built)

1. **Cross-job query memory.** Before planning, load the most productive query patterns from past jobs with a similar ICP (from `discovery_queries`) and seed the planner with them.
2. **Lookalike expansion.** From each qualified lead, search for named competitors and peers ("brands like X", "X vs", case-study customer lists) and feed them back as candidates.
3. **Counter-evidence pass on borderline leads (score 60-75).** A second judge call looks for disqualifiers ("already runs its own warehouse", "acquired", "closing") using only verified evidence; a borderline lead qualifies only if none are found.
4. **Recency-weighted intent in code.** Compute a decay weight from each signal's effective date (e.g. 1.0 at 0-14 days → 0.5 at 90 days) and give it to the judge, so freshness is consistent and not left to the model.
5. **Self-consistency on borderline scores.** Score twice and keep the lower score (cheap on Terra; removes lucky scores).
6. **Prompt-cache-friendly layout.** Static instructions, ICP and rubric first, company evidence last, to get OpenAI's cached-input price (Luna $0.01/M, Terra $0.05/M).
7. **Eval set from pilots.** Hand-graded leads ("would pitch" / "wouldn't") become a fixed test set; prompts and thresholds are tuned against it, not by feel.
8. **Account-level signals from free official sources.** Companies House filing history (new accounts filed, change of directors, new charges) as dated signals.

## Rules that never change

- No bypassing bot protection, logins or CAPTCHAs; robots.txt is respected.
- No guessed emails; catch-all/unknown emails are never delivered.
- Every delivered fact has a source URL and a verified quote.
