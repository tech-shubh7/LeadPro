set dotenv-load := true

# List the recipes
default:
    @just --list

# Start the console (http://127.0.0.1:8000 by default; WEB_HOST / WEB_PORT in .env)
dev:
    uv run python -m app.server

# Run one job from a YAML spec without the console: just run jobs/example.yaml
run job:
    uv run leadpro run {{job}}

# Check configuration and provider keys (add --online to make one real call per provider)
doctor *args:
    uv run leadpro doctor {{args}}

# Auto-generate a migration from the models: just migration "add hunter columns"
migration msg="auto":
    uv run alembic revision --autogenerate -m "{{msg}}"

# Apply pending migrations
migrate:
    uv run alembic upgrade head

# Run the test suite
test *args:
    uv run pytest -q {{args}}

# Fix lint issues and format
format:
    uv run ruff check . --fix
    uv run ruff format .

# Check lint and formatting without changing anything (CI)
lint:
    uv run ruff check .
    uv run ruff format --check .

# Planner evals: real model, realistic clients, repeated. Example: just eval --repeats 5
eval *args:
    uv run python -m scripts.eval_planner {{args}}

# A standalone run worker (set EMBEDDED_WORKER=false for the web server when you use these)
worker:
    uv run python -m worker.run_worker

# Which discovery source produces qualified leads (database only, no paid calls): just sources --job 28
sources *args:
    uv run leadpro sources {{args}}

# Does the score match your lead ratings? Suggests a bar once 20+ leads are rated (database only)
calibrate:
    uv run leadpro calibrate

# Proof of selective reading for a run made with RESEARCH_CHECK=true: just research-check 29
research-check job:
    uv run leadpro research-check {{job}}

# Where a run's model spend went, per step (input / cached / output / reasoning tokens, ₹): just usage 29
usage job:
    uv run leadpro usage {{job}}
