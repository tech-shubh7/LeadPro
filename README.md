# LeadPro

Evidence-based qualified B2B lead research (UK first). Every fact comes from a page fetched during the run and is quote-checked by code. Signals must carry a trusted date inside the freshness window; when a source states an earlier event date (an old event in a new article), that date is used, and republished reports of one event count once. Emails are delivered only when verified (never catch-all, never guessed).

## Setup

```bash
uv sync
cp .env.example .env             # set DATABASE_URL (local Postgres) and API keys
createdb -h localhost -U postgres leadpro && createdb -h localhost -U postgres leadpro_test
uv run alembic upgrade head
```

API keys (`.env`, every option is documented in `.env.example`): `SERPER_API_KEY`, `GEMINI_API_KEY` or
`OPENAI_API_KEY`, `COMPANIES_HOUSE_API_KEY` (free), and at least one email finder: `GENERECT_API_KEY`,
`HUNTER_API_KEY` (tried in `EMAIL_FINDER_ORDER`; the first verified address wins).

Day-to-day commands are in the `justfile` (`just --list`):

```bash
just dev        # the console: python -m app.server  (http://127.0.0.1:8000)
just test       # the test suite
just lint       # ruff check + format check (what CI runs)
just migrate    # apply migrations
```

## Project structure

Layered design: requests flow `routes -> services -> repositories / providers`, and
nothing flows back up.

```
app/
  server.py            entry point (python -m app.server): binds, refuses unsafe public binds
  main.py              FastAPI wiring only: lifespan, middleware, error handlers, routers
  cli.py               `leadpro` CLI: scripted runs from a job YAML, doctor, compliance
  config/              settings, database engine + session dependency, templates, constants
  routes/              one router per screen (*_routes.py), thin: parse, call a service, render
  middlewares/         request_guard composes csrf -> auth -> handler -> security_headers
  exceptions/          AppError hierarchy + handlers that render every error as a page or JSON
  services/            the pipeline, one step per module (*_service.py): intake, discovery, sources,
                       research, extraction, scoring, contacts, drafts, export, run, run_manager
  repositories/        database access: lead_repository (pipeline writes), console_repository (reads)
  providers/           outside systems behind interfaces: llm/ (protocol + factory: openai, gemini,
                       manual), email/ (EmailFinder + factory: generect, hunter), search/, registry/,
                       jobs/, tenders/, web/ (page fetcher, SSRF-guarded)
  compliance/          country packs: who may be researched or contacted, and proof a company is alive
  models/              SQLAlchemy models, one table per module; enums
  schemas/             Pydantic contracts (job spec)
  utils/               small pure helpers: dates, normalise, evidence verifier, cost tracker, logger
  migrations/          Alembic
  templates/ static/   the server-rendered console
tests/                 pytest, one file per area; HTTP is mocked, the database is a real Postgres
```

## The product (web)

```bash
just dev                           # or: uv run leadpro web
```

Three screens, in the order you use them.

**1. Ask.** Enter the client's website, who they want to reach in plain words, and how many leads. That
is the whole input.

**2. Review the plan.** We read the client's own site, work out what they sell, and draft the ideal
customer profile and the buying signals to search for. Everything is editable, because a wrong profile
is the one error the quote, date and email checks downstream cannot catch. Nothing is spent on research
until you press start, and the spend cap is on the button.

**3. Watch it work.** Counters update live as each lead is verified and saved. When it finishes you get
the leads, the companies that passed every check except a verified email, the full list of what was
dropped and why, the per-provider spend, and XLSX/CSV exports.

Open any lead to see the chain behind it: each signal with its verbatim quote, how that date was
established, corroborating sources, the decision-makers with the page they were found on, and the
outreach draft with the evidence it cites.

**Compliance** is a screen, not a ticket: suppress a domain or address, or action a UK GDPR / DPDP
erasure request, in seconds.

Server-rendered FastAPI and Jinja: no build step, no CDN, no client framework. Pages are 1.4–3.1 KB
gzipped and a static bundle of 5.9 KB that is then cached permanently. Every list is paginated and every
aggregate is computed in SQL, and a test asserts a page issues the same number of queries for 18 leads
as for 3.

Runs execute as background tasks in this one process and report progress only through the database, so a
reload or a second tab shows the same truth. That is deliberate for one operator; the Postgres job queue
in `docs/RESEARCH_ENGINE.md` is what replaces it when runs must survive a restart. A crash mid-run leaves
the job marked `failed` with everything it had already saved.

The console binds to localhost and is read-only apart from starting a run and the two compliance
controls. To expose it anywhere else set `WEB_AUTH_TOKEN` (the CLI refuses to bind beyond localhost
without one) and put it behind TLS.

The CLI remains for scripted and repeatable runs from a job file: see [docs/WORKFLOWS.md](docs/WORKFLOWS.md).

## CLI (scripted runs)

```bash
uv run leadpro validate jobs/example_uk.yaml          # free check of the job file
uv run leadpro doctor --llm manual                    # database + keys for the chosen mode
uv run leadpro run jobs/example_uk.yaml --llm openai --fresh   # writes output/<client>_job<N>_<date>.xlsx + CSVs
```

There are two workflows, both described in [docs/WORKFLOWS.md](docs/WORKFLOWS.md):
- **`--llm openai`** needs `OPENAI_API_KEY` and runs unattended.
- **`--llm manual`** needs no OpenAI key. The LLM steps are answered as task files, by Claude Code (`/leadpro-manual jobs/x.yaml`) or a person (`leadpro tasks / task / answer`).

`--fresh` ignores the dev caches; use it for every client or demo run.

Outputs:
- **Leads**: qualified companies with at least one verified decision-maker email (one row per person), plus an optional personalized email.
- **ICP fit - no strong signal** (only when `include_icp_only: true`): active registered companies with strong ICP fit but no current signal strong enough to qualify. Labelled `lead_tier=icp_only`, used only to fill up to `count` after signal leads, never given outreach drafts.
- **Good leads - no contact**: passed every check except a verified decision-maker email; shows who was found and why there's no email.
- **Methodology**: how the list was built.

Each job has a cost cap (`cost_cap_inr` in the job file, default `MAX_COST_PER_JOB_INR`). The run stops cleanly when it's reached.

## Tests

```bash
uv run pytest        # offline, free: uses the leadpro_test database and fake providers
```

Open decisions and unverified facts: `docs/OPEN_ITEMS.md`.
