# LeadPro — Open Items

Every pending decision and unverified fact lives here, in one place. Update this file whenever an item is settled: move it to **Settled** at the bottom with the date and the evidence.


---

## 1. Decisions deferred by the owner (not blocking the research engine)

| # | Decision | Notes |
|---|---|---|
| D1 | Pricing for clients: setup fee, per lead, per meeting | Research later. The product is done-for-you, so the tool is internal. |
| D2 | Email sending: whose domains, which sending tool, when | The research engine comes first. v3 rules in plan §8b remain the default. |
| D3 | Definition of a "qualified held meeting" (for any per-meeting billing) | Needed only if per-meeting pricing is offered. Must state: matches the ICP, with a decision-maker, held, minimum length. |
| D4 | Paid decision-maker phone lookup: which provider (Datagma vs others for UK/US) | Add-on, paid for by the client. Free published phones are always included. |
| D5 | Directors' phones from public PDFs (India) | Needs a lawyer's opinion. Off by default. India is parked anyway. |
| D6 | Hosting: laptop vs small server | A server is needed only once weekly watch mode exists. |
| D7 | What to do with `prospects/` (45 Gujarat leads built partly with the LinkedIn guest feed, which is now banned) | Kept as reference only, not imported. |
| D8 | Defaults: 90-day freshness, score ≥ 70, 2 decision-makers per company, ICP-only fit ≥ 32/40, email reuse ≤ 60 days | Keep them, then tune after pilots. |
| D9 | **Exclusive vs non-exclusive leads.** Today every lead is non-exclusive: never-repeat is per client, and two clients can receive the same company. | Decide with pricing (D1). Until then, tell clients up front that leads are non-exclusive. Don't sell exclusivity until the database enforces it (a reservation table: client, company, scope/territory, start, expiry; checked before delivery). |
| D10 | **Repeat delivery when a materially new signal appears** (e.g. an RFP months after a no-signal delivery) | Needed with weekly watch mode. Options per client: never repeat (current), repeat after N days, repeat on a new event_key, or manual approval. |

## 2. Facts not yet verified (settle with free credits before the matching adapter goes live)

| # | Unknown | How to settle | Blocks |
|---|---|---|---|
| F1 | Serper: whether news/organic items include a `date` field (code tolerates it missing); `/places` field names (Maps not used yet; if used, only as a discovery pointer, never stored or delivered, per Google Maps terms); whether `tbs=qdr:*` works on `/news`; quality of UK/US job results (Reed, Indeed) | Serper free tier (2,500 queries) | `providers/search/serper.py` |
| F2 | Generect hit rate on UK decision-makers; confirm live responses match the OpenAPI spec (`valid_email`, `result`, `catch_all`); find out what `source: FORMAT` results look like in practice | Free starter credit, then a $20 top-up | `providers/email/generect.py` |
| F3 | **Is one email provider enough?** Cross-check the first ~100 Generect `valid` emails with MillionVerifier (100 free credits). Add a second check if it disagrees on more than 3%, or if pilot hard-bounces exceed 2%. | Free credits plus pilot bounces | Email quality guarantee |
| F9 | **Waterfall trigger:** if fewer than ~60% of confirmed decision-makers get a verified email in pilots, add a second finder (it must bill only for found emails) | Pilot metrics: `qualified` vs `qualified_no_contact` counts | Contact coverage |
| F10 | How often UK name matching to Companies House fails (brand vs registered name), so leads end up `outreach_allowed=no` | Pilot audit of the `registry_id` / `registry_match` columns. Since 2026-10-31 the matcher tries, in order: company number printed on the site, registered name printed on the site (must belong to the site's domain; agency/supplier credits ignored), brand name. Each name step needs exactly one active exact match | UK outreach coverage |
| F4 | Companies House: exact fields, current rate limit (600 per 5 minutes is not confirmed officially), officers endpoint | Free API key plus the official docs | `providers/registry/companies_house.py` |
| F5 | Whether web-only alive proof is enough for US companies (no national registry; OpenCorporates is a £2,250+/yr subscription, so it's excluded; SEC EDGAR covers listed companies only) | Pilot audit | US Country Pack |
| F6 | GPT-6.1 Terra prompt-injection resistance and judging quality vs Opus 5.5 / Luna-max | Phase 2 blind test, about ₹115 | Confidence in the judge model |
| F7 | `gpt-6-luna` structured outputs at `reasoning_effort="low"` reliably follow our JSON schema | First real calls | `providers/llm/openai_provider.py` |
| F8 | FileSure master-data fields; gstinapi.in response format | Only when India is reactivated | India Country Pack |

## 3. Answered only by real campaigns or pilots

| # | Unknown |
|---|---|
| R1 | Reply, meeting and show rates for our personalized emails (published averages are about 3–4% replies) |
| R2 | Candidate drop-off per country (India hand run: about 50%) |
| R3 | Real cost per qualified lead per country |
| R4 | Whether Indian companies that sell abroad will pay (hypothesis) |
| R5 | Quality metrics to track per pilot (not volume): duplicate rate, contact accuracy (bounces), signal accuracy (spot-checked), ICP match rate, client acceptance rate, meeting rate, and signal-tier vs ICP-only-tier performance |

## 4. Legal questions

| # | Question |
|---|---|
| L1 | UK GDPR: write a one-page legitimate-interest assessment before the first UK outreach |
| L2 | US CAN-SPAM: whose postal address goes in each email (the client's or ours)? |
| L3 | US state privacy laws (e.g. California) covering B2B contact data. Believed to apply since 2023, but not verified. |
| L4 | Client contract template: sender of record, data-processing terms. Also ask: when the same named person is delivered to several clients, what is our role (controller for the onward sharing?), and what notice to the person is needed? Publicly visible business contacts are still personal data under UK GDPR. |

## 5. Technical risks

| # | Risk | Mitigation |
|---|---|---|
| T1 | Sites behind bot protection (Cloudflare etc.) | We never bypass it. Count it as drop-off. |
| T2 | Job posts give "N days ago" rather than a date | Store the approximate date with `date_precision="relative"`. This is fine for a 90-day window. |
| T3 | Quote check on pages built with JavaScript | Playwright fallback; the stored text that was checked is the text that was rendered |
| T4 | Cost cap accuracy | Per-call providers are counted by us; LLM cost comes from returned usage |

---

## 6. Data-handling hardening (done 2026-10-30)

- **Incremental persistence.** Each successfully-processed lead is saved to the database inside its own worker, immediately, via its own `session_scope()` — not batched at the end of the job. A crash partway through a run cannot lose already-computed leads.
- **Honest failure handling.** Any unexpected error (not a per-candidate `Rejected`/`CostCapExceeded`, which are already handled per-candidate) marks the job `FAILED` with the Python traceback in `jobs.error`, and still attempts to export/record whatever was already persisted before the crash (`_finalize` is called once on the normal path and again, defensively, from the failure path). A job is never left stuck in a running status.
- **Concurrency-safe upserts.** `Company`, `Contact`, `JobLead` and `Delivery` get-or-create through `store._get_or_create`, which uses a SAVEPOINT (`session.begin_nested()`) and recovers from a concurrent-insert race instead of raising `IntegrityError`. Proven with a mutation test (reverting the fix makes the test fail with a real `UniqueViolation`).
- **Suppression list wired into the pipeline**, not just stored. Suppressed domains are excluded from discovery (`exclude` set in `run_job`); suppressed emails are refused at all three attachment points in `attach_emails` (known-reuse cache, printed-on-page, and finder result).
- **`leadpro suppress <domain|email> <value> --reason ...`** and **`leadpro erase-person <email> --reason ...`** CLI commands. Erasure deletes the `Contact` row (UK GDPR / DPDP) and adds a permanent suppression so a later finder lookup or cache reuse can't resurrect it; the `Delivery` row survives with `contact_id` set to `NULL` (FK `ondelete="SET NULL"`), so never-repeat dedup for that company still holds.
- **25 database-level CHECK constraints** (added in an earlier pass) are now proven by raw-SQL tests in `tests/test_db_constraints.py` (24 tests) that bypass the ORM's own validators entirely — so they hold even against a bug in the Python code, not just by convention.

## 7. Second-opinion review, adopted items (done 2026-10-30)

From an external lead-generation playbook, checked against the code. Most of it was already built, often more strictly. These gaps were fixed:

- **Event date vs publication date.** Extraction copies the stated event date phrase ("in March 2025"). Code accepts it only if it appears verbatim in the same source within 600 characters of the signal quote, and parses it itself, taking the end of the stated period. Freshness uses the earlier of the event date and the publication date. A stated date can only make a signal older; it never dates an undated source. Stored as `evidence.event_date` and `event_date_quote` (the database requires the phrase whenever there's a date). The export adds `signal_N_date_basis`.
- **Duplicate events.** Each signal carries an `event_key`. Reports of the same event (same key, or the same claim) collapse to the earliest-dated one, and the others are kept as `corroborating_urls`. Merging only removes signals, so it can't inflate intent.
- **ICP-only tier (opt-in, `include_icp_only`).** A company with no qualifying signal is delivered only if it's alive (in the UK that means active on Companies House) and its fit is ≥ `ICP_ONLY_MIN_FIT` (32/40). It goes on its own sheet labelled `lead_tier=icp_only`, never gets an outreach draft, and only fills `count` after signal leads. `job_leads.lead_tier` is required for every non-rejected lead.
- **Identity by registry number.** A company number printed on the company's own site resolves any second domain to the existing company record. The run rejects the second domain as a "duplicate company". A company delivered under one domain isn't re-delivered under another. A partial unique index allows one record per registered company. Name-only matches never merge records.
- **Judge gets the playbook.** Signal meanings and `strength` go to the judge, with the rule that an event isn't automatically a buying need.
- **Email reuse window** reduced from 90 to 60 days (`EMAIL_REUSE_MAX_DAYS`).
- **Not adopted:** the 10-table schema, tenant isolation, the Gulf/Australia legal rows, and unverified legal dates (India DPDP "13 May 2027", Oman decree). They don't apply to an internal UK-first tool.

## 8. Structured sources (added 2026-11-01)

Discovery can start from public datasets in which every row is already a candidate buyer, instead of general web
search (`sources:` in the job YAML, `sources_only: true` to skip web search). Built and tested:

- **ats_jobs**: Greenhouse, Lever, Ashby and SmartRecruiters public job APIs (endpoints checked live). Boards are found
  with `site:` Google queries; postings are filtered by role words, location and age. The platform's own first-published
  date is the signal date (`job_posting`); Greenhouse `updated_at` is deliberately not used. Workable, Teamtailor and
  Recruitee are NOT supported (no verified public endpoint).
- **contracts_awards**: UK Contracts Finder award notices (OCDS). The notice names the supplier and usually its Companies
  House number, used as an identity-grade match (`company_number_in_source`) and cross-checked against any number printed
  on the website. Publication date is stored as `official_notice` (migration `a41c7e2d9b10`).

Measured fit (2026-11-01 dry run, 8 Google queries, 49 boards, ~70s): good for buyers that run a formal ATS (B2B SaaS and
scale-ups hiring sales/RevOps/engineers). Poor for small UK agencies and SMEs: the 22 candidates it returned for
"agencies hiring business development" were large software firms (Anthropic, Braze, Spotify, Samsara), because small UK agencies
rarely use these platforms. For SME/agency ICPs use news-driven discovery or seeds; keep this source for ICPs that fit it.

Designed, NOT built:
- **Pool sources** (Companies House SIC search, FCA register, Ofsted, CQC): complete lists with no dated event; they need a
  trigger per company, so they only feed the `include_icp_only` tier until a trigger step exists.
- **Runtime source proposal** (the model proposes which public dataset lists a new client's buyers, code validates that the
  URL exists, is structured, is dated and is permitted by robots.txt/terms before use). Needs a per-source parser contract
  first, otherwise it would reintroduce hallucination at the discovery layer.
- **Not usable under their terms**: Clutch, ThomasNet, ImportYeti (bulk extraction prohibited). BuiltWith is paid API only.
  SAM.gov and NMLS are US datasets (US pack).

## Settled

| Date | Item | Outcome / evidence |
|---|---|---|
| 2026-10-30 | Second-opinion review | Event dates + duplicate-event collapse, opt-in ICP-only tier, registry-number identity, playbook strengths for the judge, 60-day email reuse. See §7. Migration `bffbd3780c78`. |
| 2026-10-30 | Maps data in deliverables | Never. Phones/emails come only from the company's own pages; Maps (if ever used) is a discovery pointer only |
| 2026-10-30 | Data-handling hardening | Incremental per-lead persistence, failure handling that marks jobs FAILED with the error recorded, SAVEPOINT-based concurrency-safe upserts, suppression list wired into discovery + email attachment, `suppress`/`erase-person` CLI commands, 24 raw-SQL tests proving DB constraints hold independent of the ORM. See §6. |
| 2026-10-30 | Agentic discovery | Query planning with yield feedback, batch triage of hits, prior-ranked research, per-company news search, blocked-site fallback, dev Serper cache, verified-email reuse. Design: `docs/RESEARCH_ENGINE.md` |
| 2026-10-30 | Bot-protected sites | Never bypassed. Researched from news, registry and search results, flagged in `website_access` |
| 2026-10-30 | Hybrid mode until an OpenAI key or paid pilot | `LLM_MODE=manual`: LLM steps are answered by Claude via `work/llm_tasks/` files; same schemas and quote checks; switch back with `LLM_MODE=openai`. Local Postgres on 5432 replaces Docker. |
| 2026-10-30 | Email finder | **Generect** (PAYG, $0.02 per found email via API, free on misses, no subscription). Fields verified from its OpenAPI spec. OrbiSearch dropped (charges for misses). |
| 2026-10-30 | Waterfall | Not built for the MVP; the interface supports it; trigger is F9 |
| 2026-10-30 | Good leads with no verified contact | Delivered in a separate sheet/CSV ("Good leads - no contact"), status `qualified_no_contact` |
| 2026-10-30 | Companies House API | Paths, fields, Basic auth and officer roles verified from official specs/enumerations |
| 2026-10-30 | Simplification | 3 paid providers (Serper, OpenAI, OrbiSearch) plus free Companies House; Postgres with 6 tables; 6-step pipeline; Anthropic removed |
| 2026-10-30 | OrbiSearch billing | **Charges for misses.** Bulk-lookup docs say "every returned row is charged"; a GitHub treg PR #1246 live test shows 1 credit on a miss. Mitigation: call it only for confirmed decision-makers on MX-valid domains. |
| 2026-10-30 | Alternative finders that charge only for found emails | Anymail, Findymail and Hunter need subscriptions; Mailsfinder's API needs a subscription or a $249 one-time Lifetime plan. Not worth switching at pilot scale. |
| 2026-10-30 | Market | UK → US → India; Country Pack architecture |
| 2026-10-30 | Product | Lead list (XLSX/CSV) with reasons; optional personalized email per lead |
| 2026-10-30 | Phones | Free published phones included; paid lookup is a client add-on |
| 2026-10-29 | Judge model | GPT-6.1 Terra (owner's choice) |
| 2026-10-29 | Email providers | OrbiSearch + MillionVerifier (Anymail rejected: smallest one-time pack is $1,090) |
| 2026-10-29 | Google News RSS | Rejected: feed terms forbid commercial use |
| 2026-10-29 | LinkedIn guest jobs feed | Removed from the pipeline (LinkedIn ToS) |
