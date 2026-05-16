"""Wrapper around the gh CLI for PR creation and run log fetching.

We shell out to `gh` rather than using PyGithub or Octokit directly:
- It's simpler (no auth wiring, gh inherits user/CI credentials)
- It works in both local and webhook contexts
- It's the same approach we used in v1, which was battle-tested

If gh is not installed or not authenticated, functions raise GhCliError
with a clear remediation message.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path


class GhCliError(RuntimeError):
    """Raised when gh CLI is unavailable or returns an error."""


@dataclass
class FetchedRun:
    """A failed GitHub Actions run with its logs persisted locally."""

    repo: str
    run_id: str
    workflow_name: str | None
    log_path: Path


async def is_gh_available() -> bool:
    """Check if gh is installed and authenticated.

    Returns True if both `gh --version` succeeds AND `gh auth status` succeeds.
    """
    if not shutil.which("gh"):
        return False
    proc = await asyncio.create_subprocess_exec(
        "gh", "auth", "status", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    return proc.returncode == 0


async def resolve_latest_failed_run(repo: str) -> str:
    """Find the most recent failed workflow run ID for a repo.

    Args:
        repo: Repo slug, e.g. "owner/repo".

    Returns:
        Run ID (string).

    Raises:
        GhCliError: If no failed runs found or gh fails.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh", "run", "list",
        "--repo", repo,
        "--status", "failure",
        "--limit", "1",
        "--json", "databaseId",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise GhCliError(f"gh run list failed: {stderr.decode()}")
    runs = json.loads(stdout)
    if not runs:
        raise GhCliError(f"No failed runs found for {repo}")
    return str(runs[0]["databaseId"])


async def fetch_run_logs(repo: str, run_id: str, output_path: Path) -> FetchedRun:
    """Fetch failed-step logs for a GitHub Actions run.

    Args:
        repo: Repo slug, e.g. "owner/repo".
        run_id: GitHub Actions run ID.
        output_path: Path where logs will be written.

    Returns:
        FetchedRun with metadata (workflow name, log path).

    Raises:
        GhCliError: If gh fails for any reason.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh", "run", "view", run_id,
        "--repo", repo,
        "--log-failed",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise GhCliError(f"gh run view failed: {stderr.decode()}")
    output_path.write_bytes(stdout)

    # Get metadata
    meta_proc = await asyncio.create_subprocess_exec(
        "gh", "run", "view", run_id,
        "--repo", repo,
        "--json", "workflowName",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    meta_out, _ = await meta_proc.communicate()
    workflow_name: str | None = None
    if meta_proc.returncode == 0:
        try:
            workflow_name = json.loads(meta_out).get("workflowName")
        except json.JSONDecodeError:
            pass

    return FetchedRun(
        repo=repo,
        run_id=run_id,
        workflow_name=workflow_name,
        log_path=output_path,
    )


async def create_pull_request(
    repo: str,
    branch_name: str,
    title: str,
    body: str,
    repo_path: Path,
    draft: bool = True,
) -> str:
    """Create a branch, commit current diff, push, and open a PR.

    Args:
        repo: Repo slug, e.g. "owner/repo".
        branch_name: Branch name to create.
        title: PR title.
        body: PR body in Markdown.
        repo_path: Local path to the repo (must already have changes staged
            or in working tree — this function will `git add -A` and commit).
        draft: Whether to mark the PR as draft.

    Returns:
        PR URL.

    Raises:
        GhCliError: If any step fails.
    """
    cmds = [
        ["git", "checkout", "-b", branch_name],
        ["git", "add", "-A"],
        ["git", "commit", "-m", f"fix: {title}"],
        ["git", "push", "-u", "origin", branch_name],
    ]
    for cmd in cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise GhCliError(f"{' '.join(cmd)} failed: {stderr.decode()}")

    pr_args = [
        "gh", "pr", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--head", branch_name,
    ]
    if draft:
        pr_args.append("--draft")

    proc = await asyncio.create_subprocess_exec(
        *pr_args,
        cwd=str(repo_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise GhCliError(f"gh pr create failed: {stderr.decode()}")

    return stdout.decode().strip()
