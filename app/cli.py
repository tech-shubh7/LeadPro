import asyncio
import json
from pathlib import Path

import typer

from app.schemas.job_spec_schema import load_job
from app.services.run_service import run_job
from app.utils.logger import configure_logging

cli = typer.Typer(help="LeadPro: evidence-based qualified B2B lead research")


def _preflight(llm_mode: str, countries: list[str], online: bool = False) -> list[tuple[bool, str]]:
    """(ok, message) checks: database at the latest migration, and the keys this run will need."""
    from sqlalchemy import text

    from app.config.database import engine
    from app.config.settings import get_settings

    st = get_settings()
    checks: list[tuple[bool, str]] = []
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        checks.append(
            (
                current == head,
                f"database reachable, migration {current} (latest {head})"
                + ("" if current == head else ": run `uv run alembic upgrade head`"),
            )
        )
    except Exception as exc:
        checks.append((False, f"database: {type(exc).__name__}: {exc}"))
    checks.append((bool(st.serper_api_key), "SERPER_API_KEY set (search)"))
    if "GB" in countries:
        checks.append((bool(st.companies_house_api_key), "COMPANIES_HOUSE_API_KEY set (free UK registry)"))
    searchers = [n for n in st.search_provider_order if getattr(st, f"{n}_api_key", "")]
    checks.append(
        (bool(searchers), f"search providers with a key, in fallback order: {', '.join(searchers) or 'none'}")
    )
    checks.append(
        (
            True,
            "TINYFISH_API_KEY set: page rendering fallback and browse sources on"
            if st.tinyfish_api_key
            else "TINYFISH_API_KEY not set: no rendering fallback or browse sources",
        )
    )
    finders = [n for n in st.email_finder_order if getattr(st, f"{n}_api_key", "")]
    checks.append(
        (bool(finders), f"email finders with a key, in waterfall order: {', '.join(finders) or 'none'}")
    )
    if llm_mode == "gemini":
        checks.append((bool(st.gemini_api_key), "GEMINI_API_KEY set (llm mode 'gemini')"))
        if online and st.gemini_api_key:
            from google import genai
            from google.genai import types as genai_types

            # A real generate, not models.get(): metadata lookup succeeds for models the key is not
            # allowed to call, so it passes for a retired model and the run then dies on a 404.
            client = genai.Client(
                api_key=st.gemini_api_key, http_options=genai_types.HttpOptions(api_version="v1beta")
            )
            for model in {st.gemini_extract_model, st.gemini_judge_model}:
                try:
                    client.models.generate_content(
                        model=model,
                        contents="ok",
                        config=genai_types.GenerateContentConfig(max_output_tokens=800),
                    )
                    checks.append((True, f"Gemini model {model} answers"))
                except Exception as exc:
                    checks.append((False, f"Gemini model {model}: {type(exc).__name__}: {str(exc)[:200]}"))
    elif llm_mode == "openai":
        checks.append(
            (
                bool(st.openai_api_key),
                "OPENAI_API_KEY set (llm mode 'openai'; use --llm manual without a key)",
            )
        )
        if online and st.openai_api_key:
            from openai import OpenAI

            for model in {st.llm_extract_model, st.llm_judge_model}:
                try:
                    OpenAI(api_key=st.openai_api_key).models.retrieve(model)  # free call
                    checks.append((True, f"OpenAI model {model} available"))
                except Exception as exc:
                    checks.append((False, f"OpenAI model {model}: {type(exc).__name__}: {exc}"))
    else:
        checks.append(
            (True, "llm mode 'manual': LLM steps are answered in work/llm_tasks (see docs/WORKFLOWS.md)")
        )
    return checks


def _resolve_mode(llm: str | None) -> str:
    from app.config.settings import get_settings
    from app.providers.llm.llm_factory import LLM_MODES

    mode = llm or get_settings().llm_mode
    if mode not in LLM_MODES:
        typer.echo(f"--llm must be one of {', '.join(LLM_MODES)}")
        raise typer.Exit(2)
    return mode


@cli.command()
def doctor(
    llm: str = typer.Option(None, "--llm", help="openai | gemini | manual (default: LLM_MODE from .env)"),
    online: bool = typer.Option(
        False, "--online", help="Also confirm the OpenAI models exist (free API call)"
    ),
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
    llm: str = typer.Option(
        None,
        "--llm",
        help="openai | gemini | manual (no key: answer tasks with "
        "`leadpro task`/`leadpro answer`). Default: LLM_MODE",
    ),
    fresh: bool = typer.Option(
        False,
        "--fresh",
        help="Ignore dev caches: live searches and pages (24h page cache). "
        "Use for client/demo runs; dev caches are for debugging reruns.",
    ),
) -> None:
    """Run a job described by a YAML file and write XLSX + CSV outputs."""
    configure_logging()
    spec = load_job(job_file)
    mode = _resolve_mode(llm)
    if fresh:
        from app.config.settings import get_settings

        get_settings().app_env = "prod"  # before any provider is built: Serper/page caches follow is_dev
    failed = [msg for ok, msg in _preflight(mode, spec.countries) if not ok]
    if failed:
        typer.echo("Not ready:\n  " + "\n  ".join(failed))
        raise typer.Exit(1)
    typer.echo(
        f"LLM mode: {mode}" + (" (answer tasks: `uv run leadpro tasks --wait`)" if mode == "manual" else "")
    )
    summary = asyncio.run(run_job(spec, out_dir, llm_mode=mode))
    typer.echo(f"Job {summary.job_id}: {summary.status.value}")
    typer.echo(f"  qualified (verified contact): {summary.qualified}")
    typer.echo(f"  ICP fit, no strong signal (opt-in tier): {summary.icp_only}")
    typer.echo(f"  watchlist (fresh sign, score just under the bar): {summary.watchlist}")
    typer.echo(f"  good leads, no verified contact: {summary.no_contact}")
    typer.echo(f"  rejected: {summary.rejected}")
    typer.echo(f"  cost: ₹{summary.cost_inr}")
    for name, path in summary.outputs.items():
        typer.echo(f"  {name}: {path}")
    if summary.status.value == "failed":
        raise typer.Exit(1)


@cli.command()
def tasks(
    wait: int = typer.Option(0, "--wait", help="Seconds to wait for a task to appear (0 = don't wait)"),
) -> None:
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
    typer.echo(
        f"# {t['id']} — {t['step']} (role model: {t['role_model']})\n\n## Instructions\n{t['instructions']}"
    )
    typer.echo(f"\n## Answer JSON schema\n{json.dumps(t['json_schema'])}\n\n## Data\n{t['data']}")


@cli.command()
def answer(
    task_id: str,
    answer_file: Path,
    force: bool = typer.Option(False, "--force", help="Write even if quote pre-checks fail"),
) -> None:
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
def web(
    host: str | None = typer.Option(
        None, help="Bind address (default WEB_HOST). Localhost unless WEB_AUTH_TOKEN and TLS are set."
    ),
    port: int | None = typer.Option(None, help="Port (default WEB_PORT)."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (development only)"),
    tls_certfile: Path | None = typer.Option(
        None, "--tls-certfile", help="PEM certificate required for a public bind."
    ),
    tls_keyfile: Path | None = typer.Option(
        None, "--tls-keyfile", help="PEM private key required for a public bind."
    ),
) -> None:
    """Serve the operator console: every run, lead and piece of evidence, plus suppression and erasure."""
    from app.server import UnsafeBindError, serve

    try:
        serve(host, port, reload, tls_certfile, tls_keyfile)
    except UnsafeBindError as exc:
        typer.echo(str(exc))
        raise typer.Exit(2) from exc


@cli.command("feedback-export")
def feedback_export(
    out: Path = typer.Option(Path("evals/lead_feedback.jsonl"), help="Where to write the dataset"),
) -> None:
    """Write every rated lead, with the evidence it was delivered on, as one JSON object per line."""
    from app.config.database import session_scope
    from app.repositories import feedback_repository

    with session_scope() as s:
        rows = feedback_repository.export_rows(s)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(row, default=str) + "\n" for row in rows))
    typer.echo(f"{len(rows)} rated lead(s) written to {out}")


@cli.command()
def validate(job_file: Path) -> None:
    """Check a job YAML without spending anything."""
    spec = load_job(job_file)
    typer.echo(
        f"OK: {spec.client.name}, {spec.count} leads in {spec.countries}, {len(spec.playbook.signals)} signals"
    )


@cli.command()
def suppress(
    kind: str = typer.Argument(..., help="'domain' or 'email'"),
    value: str = typer.Argument(...),
    reason: str = typer.Option(
        ..., "--reason", help="Why this is suppressed (opt-out, erasure request, competitor...)"
    ),
) -> None:
    """Add a domain or email to the suppression list: never researched / looked up / delivered again."""
    from app.config.database import session_scope
    from app.repositories import lead_repository as store

    if kind not in ("domain", "email"):
        typer.echo("kind must be 'domain' or 'email'")
        raise typer.Exit(1)
    with session_scope() as s:
        store.add_suppression(s, kind, value, reason)
    typer.echo(f"Suppressed {kind}: {value.strip().lower()}")


@cli.command()
def sources(
    job: list[int] = typer.Option(None, "--job", help="Job id (repeat for several; default: every job)"),
) -> None:
    """Which discovery source finds companies that qualify: researched, skipped free, near misses,
    qualified and rated good, per channel. Reads the database only; makes no paid call."""
    from app.config.database import session_scope
    from app.repositories import console_repository as repo

    with session_scope() as s:
        report = repo.source_performance(s, job or None)
    head = (
        f"{'source':<18}{'researched':>11}{'skipped':>9}{'scored':>8}{'near':>6}{'watch':>7}"
        f"{'qualified':>11}{'good':>6}{'cost':>10}{'tokens':>10}{'per kept':>10}"
    )
    typer.echo(head)
    for r in report.rows:
        per_kept = f"₹{r.cost_per_kept:.2f}" if r.cost_per_kept is not None else "-"
        cost = f"₹{r.cost_inr:.2f}"
        typer.echo(
            f"{r.channel:<18}{r.found:>11}{r.skipped_before_model:>9}{r.scored:>8}{r.near_misses:>6}"
            f"{r.watchlist:>7}{r.qualified:>11}{r.approved:>6}{cost:>10}{r.tokens:>10}{per_kept:>10}"
        )
    per = report.cost_per(report.qualified)
    untagged = f"; ₹{report.untagged_cost_inr:.2f} from untagged runs" if report.untagged_cost_inr else ""
    typer.echo(
        f"cost ₹{report.cost_inr:.2f} (finding candidates ₹{report.shared_cost_inr:.2f}, shared{untagged})"
        + (f" · ₹{per:.2f} per qualified lead" if per is not None else "")
    )


@cli.command("research-check")
def research_check(
    job: int = typer.Argument(..., help="Job id of a run made with RESEARCH_CHECK=true"),
) -> None:
    """Proof of selective reading on one run: tokens of the selective vs the full reading of the same
    companies, companies skipped free that had a fresh signal, and pages where a signal was lost."""
    import json

    from app.config.database import session_scope
    from app.models import Job
    from app.services.research_check_service import report

    with session_scope() as s:
        found = s.get(Job, job)
        if found is None:
            typer.echo(f"No job {job}")
            raise typer.Exit(1)
        result = report(found)
    if not result["companies"]:
        typer.echo("This run has no research check (run it with RESEARCH_CHECK=true).")
        raise typer.Exit(1)
    typer.echo(json.dumps(result, indent=1))


@cli.command()
def calibrate() -> None:
    """Does the score predict what you rate good? Precision per score band from lead ratings, and a
    suggested bar once there are enough ratings. Reads the database only; changes nothing."""
    from app.config.database import session_scope
    from app.config.settings import get_settings
    from app.repositories import feedback_repository as fb

    with session_scope() as s:
        report = fb.calibration(s, get_settings().qualify_score)
    typer.echo(
        f"{'score':<10}{'good':>6}{'wrong co.':>11}{'wrong int.':>12}{'wrong contact':>15}{'good %':>8}"
    )
    for b in report.bands:
        share = f"{b.precision:.0%}" if b.precision is not None else "-"
        typer.echo(
            f"{b.label:<10}{b.good:>6}{b.wrong_company:>11}{b.wrong_intent:>12}{b.wrong_contact:>15}{share:>8}"
        )
    for note in report.notes:
        typer.echo(f"- {note}")
    if report.suggested is not None:
        typer.echo(f"Suggested bar: {report.suggested} (current {report.threshold}); nothing was changed.")


@cli.command("erase-person")
def erase_person(
    email: str,
    reason: str = typer.Option(..., "--reason", help="Why this person's data is being erased"),
) -> None:
    """Delete a decision-maker's stored contact record by email (UK GDPR / DPDP erasure) and suppress
    the email going forward, so it cannot be re-added by a later finder lookup or cache reuse."""
    from app.config.database import session_scope
    from app.repositories import lead_repository as store

    with session_scope() as s:
        n = store.erase_person_by_email(s, email, reason)
    typer.echo(f"Erased {n} contact record(s) for {email.strip().lower()}; suppressed going forward")


if __name__ == "__main__":
    cli()
