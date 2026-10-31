import asyncio
import json
import logging
from pathlib import Path

import typer

from app.pipeline.jobspec import load_job
from app.pipeline.runner import run_job

cli = typer.Typer(help="LeadPro: evidence-based qualified B2B lead research")


def _preflight(llm_mode: str, countries: list[str], online: bool = False) -> list[tuple[bool, str]]:
    """(ok, message) checks: database at the latest migration, and the keys this run will need."""
    from sqlalchemy import text

    from app.config import get_settings
    from app.db import engine

    st = get_settings()
    checks: list[tuple[bool, str]] = []
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        checks.append((current == head, f"database reachable, migration {current} (latest {head})"
                       + ("" if current == head else ": run `uv run alembic upgrade head`")))
    except Exception as exc:
        checks.append((False, f"database: {type(exc).__name__}: {exc}"))
    checks.append((bool(st.serper_api_key), "SERPER_API_KEY set (search)"))
    if "GB" in countries:
        checks.append((bool(st.companies_house_api_key), "COMPANIES_HOUSE_API_KEY set (free UK registry)"))
    checks.append((bool(st.generect_api_key), "GENERECT_API_KEY set (verified emails)"))
    if llm_mode == "openai":
        checks.append((bool(st.openai_api_key), "OPENAI_API_KEY set (llm mode 'openai'; use --llm manual without a key)"))
        if online and st.openai_api_key:
            from openai import OpenAI
            for model in {st.llm_extract_model, st.llm_judge_model}:
                try:
                    OpenAI(api_key=st.openai_api_key).models.retrieve(model)  # free call
                    checks.append((True, f"OpenAI model {model} available"))
                except Exception as exc:
                    checks.append((False, f"OpenAI model {model}: {type(exc).__name__}: {exc}"))
    else:
        checks.append((True, "llm mode 'manual': LLM steps are answered in work/llm_tasks (see docs/WORKFLOWS.md)"))
    return checks


def _resolve_mode(llm: str | None) -> str:
    from app.config import get_settings
    from app.pipeline.runner import LLM_MODES

    mode = llm or get_settings().llm_mode
    if mode not in LLM_MODES:
        typer.echo(f"--llm must be one of {', '.join(LLM_MODES)}")
        raise typer.Exit(2)
    return mode


@cli.command()
def doctor(
    llm: str = typer.Option(None, "--llm", help="openai | manual (default: LLM_MODE from .env)"),
    online: bool = typer.Option(False, "--online", help="Also confirm the OpenAI models exist (free API call)"),
) -> None:
    """Check the database and API keys before a run. Spends nothing."""
    mode = _resolve_mode(llm)
    checks = _preflight(mode, ["GB"], online)
    for ok, msg in checks:
        typer.echo(f"  [{'ok' if ok else 'FAIL'}] {msg}")
    if not all(ok for ok, _ in checks):
        raise typer.Exit(1)
    typer.echo(f"Ready (llm mode: {mode}).")


@cli.command()
def run(
    job_file: Path,
    out_dir: Path = Path("output"),
    llm: str = typer.Option(None, "--llm", help="openai (needs OPENAI_API_KEY) | manual (no key: answer tasks "
                                                "with `leadpro task`/`leadpro answer`). Default: LLM_MODE"),
    fresh: bool = typer.Option(False, "--fresh", help="Ignore dev caches: live searches and pages (24h page cache). "
                                                     "Use for client/demo runs; dev caches are for debugging reruns."),
) -> None:
    """Run a job described by a YAML file and write XLSX + CSV outputs."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    spec = load_job(job_file)
    mode = _resolve_mode(llm)
    if fresh:
        from app.config import get_settings
        get_settings().app_env = "prod"  # before any provider is built: Serper/page caches follow is_dev
    failed = [msg for ok, msg in _preflight(mode, spec.countries) if not ok]
    if failed:
        typer.echo("Not ready:\n  " + "\n  ".join(failed))
        raise typer.Exit(1)
    typer.echo(f"LLM mode: {mode}" + (" (answer tasks: `uv run leadpro tasks --wait`)" if mode == "manual" else ""))
    summary = asyncio.run(run_job(spec, out_dir, llm_mode=mode))
    typer.echo(f"Job {summary.job_id}: {summary.status.value}")
    typer.echo(f"  qualified (verified contact): {summary.qualified}")
    typer.echo(f"  ICP fit, no strong signal (opt-in tier): {summary.icp_only}")
    typer.echo(f"  good leads, no verified contact: {summary.no_contact}")
    typer.echo(f"  rejected: {summary.rejected}")
    typer.echo(f"  cost: ₹{summary.cost_inr}")
    for name, path in summary.outputs.items():
        typer.echo(f"  {name}: {path}")
    if summary.status.value == "failed":
        raise typer.Exit(1)


@cli.command()
def tasks(wait: int = typer.Option(0, "--wait", help="Seconds to wait for a task to appear (0 = don't wait)")) -> None:
    """Manual mode: list LLM tasks waiting for an answer (oldest first)."""
    import time

    from app.providers.llm.manual_provider import pending_tasks

    deadline = time.monotonic() + wait
    while not (pending := pending_tasks()) and time.monotonic() < deadline:
        time.sleep(2)
    for path in pending:
        task = json.loads(path.read_text())
        err = path.with_name(path.name.replace(".task.json", ".error.txt"))
        note = "  (previous answer rejected: see .error.txt)" if err.exists() else ""
        typer.echo(f"{task['id']}  {task['step']}{note}")
    typer.echo(f"{len(pending)} pending")


@cli.command()
def task(task_id: str) -> None:
    """Manual mode: print one task (instructions, expected JSON schema, untrusted data)."""
    from app.providers.llm.manual_provider import TASK_DIR

    t = json.loads((TASK_DIR / f"{task_id}.task.json").read_text())
    typer.echo(f"# {t['id']} — {t['step']} (role model: {t['role_model']})\n\n## Instructions\n{t['instructions']}")
    typer.echo(f"\n## Answer JSON schema\n{json.dumps(t['json_schema'])}\n\n## Data\n{t['data']}")


@cli.command()
def answer(task_id: str, answer_file: Path,
           force: bool = typer.Option(False, "--force", help="Write even if quote pre-checks fail")) -> None:
    """Manual mode: pre-check an answer (quotes must be verbatim in the task's sources) and submit it."""
    from app.providers.llm.manual_provider import write_answer

    problems = write_answer(task_id, json.loads(answer_file.read_text()), force=force)
    for p in problems:
        typer.echo(f"  ! {p}")
    if problems and not force:
        typer.echo("Not submitted. Fix the quotes (or --force; failing items are dropped by the pipeline).")
        raise typer.Exit(1)
    typer.echo(f"Submitted {task_id}.")


@cli.command()
def validate(job_file: Path) -> None:
    """Check a job YAML without spending anything."""
    spec = load_job(job_file)
    typer.echo(f"OK: {spec.client.name}, {spec.count} leads in {spec.countries}, {len(spec.playbook.signals)} signals")


@cli.command()
def suppress(
    kind: str = typer.Argument(..., help="'domain' or 'email'"),
    value: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason", help="Why this is suppressed (opt-out, erasure request, competitor...)"),
) -> None:
    """Add a domain or email to the suppression list: never researched / looked up / delivered again."""
    from app.db import session_scope
    from app.pipeline import store

    if kind not in ("domain", "email"):
        typer.echo("kind must be 'domain' or 'email'")
        raise typer.Exit(1)
    with session_scope() as s:
        store.add_suppression(s, kind, value, reason)
    typer.echo(f"Suppressed {kind}: {value.strip().lower()}")


@cli.command("erase-person")
def erase_person(
    email: str,
    reason: str = typer.Option(..., "--reason", help="Why this person's data is being erased"),
) -> None:
    """Delete a decision-maker's stored contact record by email (UK GDPR / DPDP erasure) and suppress
    the email going forward, so it cannot be re-added by a later finder lookup or cache reuse."""
    from app.db import session_scope
    from app.pipeline import store

    with session_scope() as s:
        n = store.erase_person_by_email(s, email, reason)
    typer.echo(f"Erased {n} contact record(s) for {email.strip().lower()}; suppressed going forward")


if __name__ == "__main__":
    cli()
