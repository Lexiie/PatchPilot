"""patchpilot CLI entry point."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import click

from cli.orchestrator import (
    GitHubRepairOptions,
    RepairOptions,
    repair_github,
    repair_local,
)
from shared import classifier, redactor

DEFAULT_POLICY = """version: 2
repair:
  max_attempts: 3
  create_pr_default: draft
  allowed_failure_types:
    - lint
    - format
    - typecheck
    - unit_test
    - dependency_config
  forbidden_failure_types:
    - environment_missing_secret
    - network_or_infra
  forbidden_paths:
    - .env*
    - secrets/**
  require_human_review_for:
    - auth/**
    - billing/**
    - payments/**
    - migrations/**
verification:
  required_commands: []
audit:
  vc_signing_did: did:key:auto
  retention_days: 90
"""


@click.group()
@click.version_option(version="2.0.0")
def cli() -> None:
    """PatchPilot — audit-grade CI repair automation."""


@cli.command()
@click.option("--repo", default=".", help="Repository path")
@click.option("--command", required=True, help="Failing command to repair")
@click.option("--budget", default=0.50, type=float, help="Max budget in USD")
@click.option("--mode", default="full", type=click.Choice(["full", "triage", "dry-run"]))
@click.option("--dry-run", is_flag=True, help="Run without modifying files")
def repair(repo: str, command: str, budget: float, mode: str, dry_run: bool) -> None:
    """Repair a failing local command."""
    options = RepairOptions(
        repo=repo, command=command, budget=budget,
        mode=mode, dry_run=dry_run,  # type: ignore[arg-type]
    )
    run = asyncio.run(repair_local(options))
    _print_run_summary(run)


@cli.command("repair-gh")
@click.option("--repo", required=True, help="GitHub repo (owner/repo)")
@click.option("--run", default="latest-failed", help="Run ID or 'latest-failed'")
@click.option("--budget", default=0.50, type=float, help="Max budget in USD")
@click.option("--create-pr", is_flag=True, help="Create draft PR after repair")
@click.option("--local-path", default=".", help="Local checkout path")
@click.option("--verify-command", default="npm test", help="Verify command")
@click.option("--mode", default="full", type=click.Choice(["full", "triage", "dry-run"]))
@click.option("--dry-run", is_flag=True, help="Run without modifying files")
def repair_gh(  # noqa: PLR0913
    repo: str,
    run: str,
    budget: float,
    create_pr: bool,
    local_path: str,
    verify_command: str,
    mode: str,
    dry_run: bool,
) -> None:
    """Repair a failed GitHub Actions run."""
    options = GitHubRepairOptions(
        repo=repo, run=run, budget=budget, create_pr=create_pr,
        local_path=local_path, verify_command=verify_command,
        mode=mode, dry_run=dry_run,  # type: ignore[arg-type]
    )
    run_obj = asyncio.run(repair_github(options))
    _print_run_summary(run_obj)


@cli.command()
@click.option("--repo", default=".", help="Repository path")
@click.option("--command", help="Command to diagnose")
@click.option("--log", help="Log file to diagnose")
def diagnose(repo: str, command: str | None, log: str | None) -> None:
    """Classify a failure without modifying files."""
    if log:
        log_content = Path(log).read_text(errors="replace")
    elif command:
        proc = subprocess.run(
            command, shell=True, cwd=repo, capture_output=True, text=True,
        )
        if proc.returncode == 0:
            click.echo("Command passed — nothing to diagnose.")
            return
        log_content = proc.stderr + "\n" + proc.stdout
    else:
        click.echo("Error: --command or --log is required", err=True)
        sys.exit(1)

    normalized = redactor.normalize_logs(log_content)
    redaction = redactor.redact_secrets(normalized)
    classification = classifier.classify_failure(redaction.redacted_text)

    click.echo("\nDiagnosis:")
    click.echo(f"  Type:           {classification.type}")
    click.echo(f"  Confidence:     {classification.confidence:.2f}")
    click.echo(f"  Risk:           {classification.risk}")
    click.echo(f"  Repairability:  {classification.repairability}")
    click.echo(f"  Likely files:   {', '.join(classification.likely_files) or 'unknown'}")
    click.echo(f"  Evidence:       {', '.join(classification.evidence) or 'none'}")
    click.echo(f"  Secrets redacted: {redaction.count}")


@cli.command()
def init() -> None:
    """Initialize PatchPilot configuration in current directory."""
    config_path = Path(".patchpilot.yml")
    if config_path.exists():
        click.echo("⚠ .patchpilot.yml already exists. Skipping.")
        return
    config_path.write_text(DEFAULT_POLICY)
    click.echo("✓ Created .patchpilot.yml")
    click.echo("  Edit this file to customize repair policy and verification commands.")


@cli.command()
def doctor() -> None:
    """Validate environment and tool setup."""
    click.echo("\nPatchPilot Doctor\n")

    checks = [
        ("git", lambda: shutil.which("git") is not None),
        ("gh CLI", lambda: shutil.which("gh") is not None),
        (".git directory", lambda: Path(".git").exists()),
        (".patchpilot.yml", lambda: Path(".patchpilot.yml").exists()),
        ("TOKENROUTER_API_KEY env", lambda: bool(os.getenv("TOKENROUTER_API_KEY"))),
    ]

    all_ok = True
    for label, check in checks:
        ok = check()
        icon = "✓" if ok else "✗"
        click.echo(f"  {icon} {label}")
        if not ok:
            all_ok = False

    # gh auth check
    if shutil.which("gh"):
        proc = subprocess.run(["gh", "auth", "status"], capture_output=True)
        if proc.returncode == 0:
            click.echo("  ✓ gh authenticated")
        else:
            click.echo("  ⚠ gh not authenticated (run `gh auth login`)")

    click.echo("\n" + ("Ready to run." if all_ok else "Some checks need attention."))


@cli.group()
def runs() -> None:
    """View run history."""


@runs.command("list")
def runs_list() -> None:
    """List all PatchPilot runs in the current repo."""
    runs_dir = Path(".patchpilot/runs")
    if not runs_dir.exists():
        click.echo("No runs found.")
        return
    entries = sorted(runs_dir.iterdir(), reverse=True)
    if not entries:
        click.echo("No runs found.")
        return
    click.echo(f"{'ID':<20} {'Status':<14} {'Mode':<10} Created")
    click.echo("─" * 65)
    for entry in entries:
        run_file = entry / "run.json"
        if not run_file.exists():
            continue
        try:
            data = json.loads(run_file.read_text())
            click.echo(
                f"{data['id']:<20} {data['status']:<14} {data['mode']:<10} "
                f"{data.get('created_at', '?')}"
            )
        except Exception:  # noqa: BLE001
            continue


@runs.command("view")
@click.argument("run_id")
def runs_view(run_id: str) -> None:
    """Show full details of a specific run."""
    run_file = Path(".patchpilot/runs") / run_id / "run.json"
    if not run_file.exists():
        click.echo(f"Run not found: {run_id}", err=True)
        sys.exit(1)
    click.echo(run_file.read_text())


def _print_run_summary(run: object) -> None:
    """Print a brief, human-friendly run summary to stdout."""
    # We pickle the Pydantic model to dict for printing
    if hasattr(run, "model_dump"):
        data = run.model_dump()  # type: ignore[attr-defined]
    else:
        data = dict(run)  # type: ignore[arg-type]

    click.echo("\nPatchPilot Repair")
    click.echo("─" * 50)
    click.echo(f"Run ID:       {data['id']}")
    click.echo(f"Mode:         {data['mode']}")
    click.echo(f"Status:       {data['status']}")
    if data.get("classification"):
        c = data["classification"]
        click.echo(f"Type:         {c['type']}")
        click.echo(f"Confidence:   {c['confidence']:.2f}")
        click.echo(f"Risk:         {c['risk']}")
    if data.get("repair"):
        r = data["repair"]
        click.echo(f"Files:        {', '.join(r['files_changed']) or 'none'}")
        click.echo(f"Summary:      {r['summary']}")
    if data.get("verification"):
        click.echo(f"Verification: {data['verification']['status']}")
    if data.get("ledger") and data["ledger"].get("totals"):
        t = data["ledger"]["totals"]
        click.echo(f"Cost:         ${t['actual_cost_usd']:.4f}")
        if t["estimated_savings_percent"] > 0:
            click.echo(f"Saved:        {t['estimated_savings_percent']:.0f}%")
    if data.get("artifacts", {}).get("report_path"):
        click.echo(f"Report:       {data['artifacts']['report_path']}")


def main() -> None:
    """Entry point invoked by `patchpilot` script."""
    cli()


if __name__ == "__main__":
    main()
