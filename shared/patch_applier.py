"""Apply unified diffs returned by the repair-agent.

The repair-agent's app.ai() call returns a string containing a ```diff
fenced block and a ```json fenced block. We extract the diff, write it
to a temp file, apply via `git apply`, and return the result.

This module is deliberately separate from agents/repair.py so it can be
tested independently with mock model output.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PatchSummary:
    """Parsed JSON summary block from the repair-agent's response."""

    success: bool
    files_changed: list[str]
    summary: str


@dataclass
class PatchApplyResult:
    files_changed: list[str]
    applied_diff: str
    summary: PatchSummary | None


_DIFF_FENCE = re.compile(r"```(?:diff|patch)?\s*\n(diff --git[\s\S]*?)\n```")
_DIFF_BARE = re.compile(r"(diff --git[\s\S]+?)(?=\n```|\n##|\n\n[A-Z]|$)")
_JSON_FENCE = re.compile(r"```json\s*\n([\s\S]*?)\n```")
_DIFF_FILES = re.compile(r"^diff --git a/(\S+) b/\S+$", re.MULTILINE)


class PatchApplyError(RuntimeError):
    """Raised when the diff cannot be applied to the working tree."""


async def apply_patch_from_response(
    repo_path: str | Path,
    response: str,
) -> PatchApplyResult:
    """Extract the diff from a model response and apply it via git.

    The model output is expected to contain:
      1. A ```diff ... ``` block with a unified diff
      2. A ```json ... ``` block with PatchSummary fields

    If the diff is empty (e.g. model couldn't fix the issue), returns a
    result with empty files_changed and the summary parsed if available.

    Args:
        repo_path: Path to the repo where the diff should apply.
        response: Raw model response text from app.ai().

    Returns:
        PatchApplyResult with applied diff content and changed file list.

    Raises:
        PatchApplyError: If the diff is non-empty but git apply fails.
    """
    diff = _extract_diff(response)
    summary = _extract_summary(response)

    if not diff.strip():
        return PatchApplyResult(files_changed=[], applied_diff="", summary=summary)

    repo = Path(repo_path)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".patch",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(diff if diff.endswith("\n") else diff + "\n")
        patch_path = tmp.name

    try:
        # First try a normal apply
        proc = await asyncio.create_subprocess_exec(
            "git", "apply", "--recount", "--whitespace=fix", patch_path,
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            # Fall back to 3-way merge
            proc2 = await asyncio.create_subprocess_exec(
                "git", "apply", "--3way", "--recount", "--whitespace=fix", patch_path,
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr2 = await proc2.communicate()
            if proc2.returncode != 0:
                raise PatchApplyError(
                    f"git apply failed: {stderr.decode()} | "
                    f"3way fallback: {stderr2.decode()}"
                )
    finally:
        Path(patch_path).unlink(missing_ok=True)

    files_changed = (
        summary.files_changed
        if (summary and summary.files_changed)
        else _extract_files_from_diff(diff)
    )

    return PatchApplyResult(
        files_changed=files_changed,
        applied_diff=diff,
        summary=summary,
    )


def _extract_diff(response: str) -> str:
    """Extract the unified diff from a model response.

    Tries fenced ```diff first, then bare diff blocks.
    """
    fenced = _DIFF_FENCE.search(response)
    if fenced:
        return fenced.group(1)
    bare = _DIFF_BARE.search(response)
    if bare:
        return bare.group(1)
    return ""


def _extract_summary(response: str) -> PatchSummary | None:
    """Extract the JSON summary block from a model response.

    Returns None if no JSON block found or if it doesn't parse.
    """
    match = _JSON_FENCE.search(response)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return PatchSummary(
        success=bool(data.get("success", False)),
        files_changed=list(data.get("filesChanged") or data.get("files_changed") or []),
        summary=str(data.get("summary", "")),
    )


def _extract_files_from_diff(diff: str) -> list[str]:
    """Extract file paths from `diff --git a/<path> b/<path>` headers."""
    seen: set[str] = set()
    out: list[str] = []
    for path in _DIFF_FILES.findall(diff):
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out
