# Running LeadPro: two workflows

Both workflows run the same pipeline: the same searches, page fetches, Companies House checks, Generect email verification, quote checks, date rules, qualification gates and output files. The only difference is who answers the LLM steps: query planning, triage, extraction, scoring and email drafts.

| | A. No OpenAI key (manual) | B. With OpenAI key |
|---|---|---|
| Who answers LLM steps | A person (or any AI assistant) via task files | OpenAI API (GPT-6 Luna extracts, GPT-6.1 Terra judges) |
| Speed | Minutes per company, and someone must be present | Unattended, about 1–3 min per company |
| LLM cost | ₹0 | About ₹2–4 per company researched |
| Best for | Demos and pilots before an API key exists | Paid pilots and volume |

Both need `SERPER_API_KEY`, `COMPANIES_HOUSE_API_KEY` and `GENERECT_API_KEY` in `.env`.

## Finding candidates: three ways, mixable in one job

| How | Job field | Best for |
|---|---|---|
| Hand-researched companies | `seeds:` (+ `seeds_only: true`) | You already know who to look at |
| Structured public datasets | `sources:` (+ `sources_only: true`) | A buyer type that leaves a public trail: companies hiring a given role (`ats_jobs`), suppliers that just won a UK contract (`contracts_awards`) |
| General web/news search | (default) | Everything else; slowest and noisiest |

Structured sources still go through every check (live website, quote verification, dated signal, Companies House, verified email). They only replace the noisy "who should we look at" step. See `docs/OPEN_ITEMS.md` section 8 for what is and is not supported.

## Before any run

```bash
uv run leadpro validate jobs/your_job.yaml        # job file is well-formed (free)
uv run leadpro doctor --llm manual                # or --llm openai [--online]
```

`doctor` checks that the database is at the latest migration and that the keys the chosen mode needs are set. `run` performs the same checks and refuses to start if one fails.

Add `--fresh` for any client or demo run. Without it, dev mode (`APP_ENV=dev`) reuses cached searches and pages forever, which is right for debugging but not for real leads.

## A. Without an OpenAI key (manual mode)

Run it in two terminals:

```bash
# terminal 1: the run waits at each LLM step
uv run leadpro run jobs/your_job.yaml --llm manual --fresh

# terminal 2: answer the steps
uv run leadpro tasks --wait 300                 # pending task ids, oldest first
uv run leadpro task <id>                        # instructions + JSON schema + data
uv run leadpro answer <id> answer.json          # pre-checks quotes, then submits
```

Notes:
- `answer` refuses to submit a quote that isn't verbatim in the task's sources.
- The pipeline re-checks every answer against the full page text anyway.
- An answer that doesn't match the schema is rejected, written to `work/llm_tasks/<id>.error.txt`, and the task stays pending.
- Answers are cached by request: re-running the same job with the same data reuses them for free. This is how to replay a demo.

## B. With an OpenAI key

1. Put `OPENAI_API_KEY=sk-...` in `.env`. Optionally set `LLM_MODE=openai` so `--llm` can be left off.
2. Check the key and models (free call), then run:

   ```bash
   uv run leadpro doctor --llm openai --online
   uv run leadpro run jobs/your_job.yaml --llm openai --fresh
   ```

The job's `cost_cap_inr` (default `MAX_COST_PER_JOB_INR`) covers search, OpenAI and email lookups together. The run stops cleanly when the cap is reached.

## Output

Files go to `output/<client>_job<N>_<date>`: an `.xlsx` plus matching CSVs.
- `Leads`: qualified, with a verified decision-maker email.
- `ICP fit - no strong signal`: only with `include_icp_only: true`.
- `Good leads - no contact`.
- `Methodology`.
