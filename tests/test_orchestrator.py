"""End-to-end orchestrator tests with mock LLM (no real model calls).

These verify the pipeline plumbing: each step runs, ledger has correct
shape, run.json/report.md/ledger.json artifacts are written, status
transitions are correct.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from cli.orchestrator import RepairOptions, repair_local


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A minimal git repo with a passing test we can later make fail."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.mark.asyncio
async def test_command_passes_returns_failed_run(tmp_repo: Path) -> None:
    """If the command exits 0, there's nothing to repair — status='failed'.

    The 'failed' status indicates "no failure to repair" because there was
    nothing to fix; the run ends without producing a patch.
    """
    options = RepairOptions(repo=str(tmp_repo), command="true", budget=0.10)
    run = await repair_local(options)
    assert run.status == "failed"


@pytest.mark.asyncio
async def test_triage_mode_terminates_at_classify(tmp_repo: Path) -> None:
    """Triage mode classifies but doesn't attempt repair."""
    # Create a failing command that triggers a 'lint' classification
    failing_script = tmp_repo / "fail.sh"
    failing_script.write_text("#!/bin/sh\necho 'ESLint found 1 error in src/x.ts' >&2\nexit 1\n")
    failing_script.chmod(0o755)

    options = RepairOptions(
        repo=str(tmp_repo),
        command="./fail.sh",
        budget=0.10,
        mode="triage",
    )
    run = await repair_local(options)
    assert run.status == "diagnosed"
    assert run.classification is not None
    assert run.classification.type == "lint"
    assert run.repair is None or not run.repair.files_changed


@pytest.mark.asyncio
async def test_dry_run_mode_skips_model_call(tmp_repo: Path) -> None:
    """Dry-run mode classifies but doesn't call the LLM."""
    failing_script = tmp_repo / "fail.sh"
    failing_script.write_text("#!/bin/sh\necho 'ESLint found 1 error' >&2\nexit 1\n")
    failing_script.chmod(0o755)

    options = RepairOptions(
        repo=str(tmp_repo),
        command="./fail.sh",
        budget=0.10,
        mode="dry-run",
    )
    run = await repair_local(options)
    assert run.status == "diagnosed"
    # No tracked files should have been modified (dry-run shouldn't touch repo)
    proc = subprocess.run(
        ["git", "diff", "--stat"], cwd=tmp_repo, capture_output=True, text=True,
    )
    assert proc.stdout.strip() == ""


@pytest.mark.asyncio
async def test_artifacts_are_written(tmp_repo: Path) -> None:
    """Each run produces ledger.json, report.md, run.json, credentials.json."""
    failing_script = tmp_repo / "fail.sh"
    failing_script.write_text("#!/bin/sh\necho 'ESLint error' >&2\nexit 1\n")
    failing_script.chmod(0o755)

    options = RepairOptions(
        repo=str(tmp_repo), command="./fail.sh", budget=0.10, mode="triage",
    )
    run = await repair_local(options)
    run_dir = tmp_repo / ".patchpilot" / "runs" / run.id

    assert (run_dir / "ledger.json").exists()
    assert (run_dir / "report.md").exists()
    assert (run_dir / "run.json").exists()
    assert (run_dir / "credentials.json").exists()


@pytest.mark.asyncio
async def test_policy_violation_aborts_pipeline(tmp_repo: Path) -> None:
    """Network failure is forbidden by default policy — should abort."""
    # Write a custom policy that's stricter
    (tmp_repo / ".patchpilot.yml").write_text(
        """version: 2
repair:
  forbidden_failure_types: [network_or_infra]
  allowed_failure_types: [lint]
"""
    )
    failing_script = tmp_repo / "fail.sh"
    failing_script.write_text("#!/bin/sh\necho 'Error: ETIMEDOUT' >&2\nexit 1\n")
    failing_script.chmod(0o755)

    options = RepairOptions(
        repo=str(tmp_repo), command="./fail.sh", budget=0.10,
    )
    run = await repair_local(options)
    assert run.status == "aborted"
    assert run.classification is not None
    assert run.classification.type == "network_or_infra"


@pytest.mark.asyncio
async def test_no_tokenrouter_key_does_not_crash(tmp_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without TOKENROUTER_API_KEY, the orchestrator should gracefully skip repair."""
    monkeypatch.delenv("TOKENROUTER_API_KEY", raising=False)

    failing_script = tmp_repo / "fail.sh"
    failing_script.write_text("#!/bin/sh\necho 'ESLint found 1 error' >&2\nexit 1\n")
    failing_script.chmod(0o755)

    options = RepairOptions(repo=str(tmp_repo), command="./fail.sh", budget=0.10)
    run = await repair_local(options)
    # Should not crash; status should reflect that no repair was applied
    assert run.status in ("failed", "patched")  # either "no patch" or "patched (no model)"
