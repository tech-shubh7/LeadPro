# LeadPro

Evidence-based qualified B2B lead research (UK first). Every fact comes from a page fetched during the run and is quote-checked by code. Signals must carry a trusted date inside the freshness window; when a source states an earlier event date (an old event in a new article), that date is used, and republished reports of one event count once. Emails are delivered only when verified (never catch-all, never guessed).

## Setup

```bash
uv sync
cp .env.example .env             # set DATABASE_URL (local Postgres) and API keys
createdb -h localhost -U postgres leadpro && createdb -h localhost -U postgres leadpro_test
uv run alembic upgrade head
```

API keys (`.env`): `SERPER_API_KEY`, `OPENAI_API_KEY`, `COMPANIES_HOUSE_API_KEY` (free), `GENERECT_API_KEY`.

## Run a job

```bash
uv run leadpro validate jobs/example_uk.yaml   # free check of the job file
uv run leadpro run jobs/example_uk.yaml        # writes output/<client>_job<N>_<date>.xlsx + CSVs
```

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
