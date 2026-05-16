"""System prompts for app.ai() calls.

Centralized so we can iterate on prompt quality without touching agent
code. Each prompt is a function that takes the structured context it needs
and returns a string ready to pass to app.ai(system=...).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shared.models import FailureClassification


CLASSIFY_FALLBACK_SYSTEM = (
    "You are a CI failure classifier. Read the log and respond with EXACTLY ONE category name "
    "from this list: lint, format, typecheck, unit_test, integration_test, dependency_config, "
    "package_lock, snapshot, build_compile, environment_missing_secret, network_or_infra, "
    "flaky_test, unknown.\n"
    "Reply with ONLY the category name. No explanation, no punctuation."
)


def build_classify_fallback_prompt(redacted_log: str) -> str:
    """Build user prompt for the fallback classifier (when patterns failed)."""
    return f"Log:\n```\n{redacted_log[:4000]}\n```"


REPAIR_SYSTEM = (
    "You are PatchPilot, an autonomous CI repair agent. Your task is to generate a unified diff "
    "that fixes a failing CI command. Follow these rules strictly:\n"
    "1. Make the MINIMAL change to fix the failure. Do not refactor unrelated code.\n"
    "2. Do not add new dependencies unless strictly necessary.\n"
    "3. Never modify files matching the listed forbidden paths.\n"
    "4. Output a valid `git apply`-compatible unified diff in a ```diff fenced block.\n"
    "5. After the diff, output a JSON block with: success (bool), filesChanged (list of strings), "
    "summary (one-sentence description).\n"
    "6. If you cannot fix the failure with high confidence, set success=false and explain why.\n"
    "Never include explanatory prose outside the diff and JSON blocks."
)


def build_repair_prompt(
    repo_path: str,
    failure_log: str,
    classification: FailureClassification,
    verify_command: str,
    budget_usd: float,
    max_attempts: int,
    forbidden_paths: list[str],
) -> str:
    """Build the user prompt for the repair-agent's app.ai() call.

    The prompt is intentionally structured to be parseable: failure context,
    constraints, output format. Model output is then split into diff +
    JSON via shared.patch_applier.
    """
    forbidden = ", ".join(forbidden_paths) if forbidden_paths else "none"
    likely = ", ".join(classification.likely_files) if classification.likely_files else "unknown"
    repo_context = _build_repo_context(Path(repo_path), classification.likely_files)
    return (
        f"# Repository\n"
        f"{repo_path}\n\n"
        f"# Failing command\n"
        f"`{verify_command}`\n\n"
        f"# Classification\n"
        f"- Type: {classification.type}\n"
        f"- Confidence: {classification.confidence:.2f}\n"
        f"- Risk: {classification.risk}\n"
        f"- Repairability: {classification.repairability}\n"
        f"- Likely files: {likely}\n\n"
        f"# Failure log (redacted)\n"
        f"```\n{failure_log[:8000]}\n```\n\n"
        f"# Repository context\n"
        f"{repo_context}\n\n"
        f"# Constraints\n"
        f"- Budget: ${budget_usd:.2f} USD maximum\n"
        f"- Max attempts: {max_attempts}\n"
        f"- Forbidden paths (NEVER modify): {forbidden}\n\n"
        f"# Output\n"
        f"Respond with a `git apply`-compatible diff in a ```diff fence, "
        f"followed by a ```json fence containing "
        f'{{"success": true|false, "filesChanged": [...], "summary": "..."}}.'
    )


def _build_repo_context(repo_path: Path, likely_files: list[str]) -> str:
    """Return a small, text-only snapshot for standalone repair prompts."""
    files = _git_files(repo_path)
    if not files:
        return "No git-tracked files available."

    likely_set = {Path(path).as_posix() for path in likely_files}

    def priority(path: str) -> tuple[int, str]:
        if path in likely_set:
            return (0, path)
        if path.startswith("tests/") or "/tests/" in path:
            return (1, path)
        if path.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb", ".java")):
            return (2, path)
        return (3, path)

    snippets: list[str] = []
    total_chars = 0
    max_total = 24_000
    max_file = 6_000

    for rel_path in sorted(files, key=priority):
        if _skip_context_file(rel_path):
            continue
        full_path = repo_path / rel_path
        try:
            if full_path.stat().st_size > max_file:
                continue
            text = full_path.read_text(errors="replace")
        except OSError:
            continue
        if "\x00" in text:
            continue
        snippet = f"## {rel_path}\n```\n{text[:max_file]}\n```"
        if total_chars + len(snippet) > max_total:
            break
        snippets.append(snippet)
        total_chars += len(snippet)

    return "\n\n".join(snippets) if snippets else "No small text files available."


def _git_files(repo_path: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _skip_context_file(path: str) -> bool:
    blocked_parts = {".git", ".patchpilot", "__pycache__", "node_modules", "dist", "build"}
    parts = set(Path(path).parts)
    if parts & blocked_parts:
        return True
    return path.endswith((
        ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".lock", ".pyc"
    ))


AUDIT_SUMMARY_SYSTEM = (
    "You are PatchPilot's audit narrator. Given a workflow execution trace, produce a concise "
    "human-readable PR description. Focus on: what failed, what was changed, why the change is "
    "minimal, what was verified. Audience: senior engineer reviewing the PR. Keep under 200 words. "
    "No marketing language."
)


def build_audit_summary_prompt(
    classification: FailureClassification,
    files_changed: list[str],
    diff_summary: str,
    verification_status: str,
    cost_usd: float,
) -> str:
    """Build the audit-agent's summary prompt for PR body generation."""
    files = ", ".join(files_changed) if files_changed else "none"
    return (
        f"Failure type: {classification.type} (confidence {classification.confidence:.2f}, "
        f"risk {classification.risk})\n"
        f"Files changed: {files}\n"
        f"Diff summary (from repair-agent): {diff_summary}\n"
        f"Verification: {verification_status}\n"
        f"Total cost: ${cost_usd:.4f}\n\n"
        f"Write a concise PR description for a reviewer."
    )
