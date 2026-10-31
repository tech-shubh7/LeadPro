import asyncio
import logging
from pathlib import Path

import typer

from app.pipeline.jobspec import load_job
from app.pipeline.runner import run_job

cli = typer.Typer(help="LeadPro: evidence-based qualified B2B lead research")


@cli.command()
def run(job_file: Path, out_dir: Path = Path("output")) -> None:
    """Run a job described by a YAML file and write XLSX + CSV outputs."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    summary = asyncio.run(run_job(load_job(job_file), out_dir))
    typer.echo(f"Job {summary.job_id}: {summary.status.value}")
    typer.echo(f"  qualified (verified contact): {summary.qualified}")
    typer.echo(f"  ICP fit, no strong signal (opt-in tier): {summary.icp_only}")
    typer.echo(f"  good leads, no verified contact: {summary.no_contact}")
    typer.echo(f"  rejected: {summary.rejected}")
    typer.echo(f"  cost: ₹{summary.cost_inr}")
    for name, path in summary.outputs.items():
        typer.echo(f"  {name}: {path}")


@cli.command()
def tasks() -> None:
    """List manual-mode LLM tasks waiting for an answer."""
    from app.providers.llm.manual_provider import TASK_DIR
    pending = sorted(TASK_DIR.glob("*.task.json")) if TASK_DIR.exists() else []
    for path in pending:
        err = path.with_name(path.name.replace(".task.json", ".error.txt"))
        typer.echo(f"{path.name}  {'(previous answer rejected, see .error.txt)' if err.exists() else ''}")
    typer.echo(f"{len(pending)} pending")


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
