"""Tests for shared.patch_applier — diff extraction and apply.

Most cases use git fixtures (real diff apply against tmp repo). The
parsing tests don't need git and run pure-Python.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from shared.patch_applier import (
    PatchApplyError,
    apply_patch_from_response,
)


def test_extracts_diff_and_summary_from_fenced_response() -> None:
    response = """Some preamble.

```diff
diff --git a/foo.txt b/foo.txt
--- a/foo.txt
+++ b/foo.txt
@@ -1 +1 @@
-old
+new
```

```json
{"success": true, "filesChanged": ["foo.txt"], "summary": "fixed"}
```
"""
    # Parsing tests use the helpers indirectly via apply with a no-op repo.
    # We just check the helpers don't crash on the response shape.
    from shared.patch_applier import _extract_diff, _extract_summary

    diff = _extract_diff(response)
    assert diff.startswith("diff --git")
    summary = _extract_summary(response)
    assert summary is not None
    assert summary.success is True
    assert summary.files_changed == ["foo.txt"]


def test_returns_empty_for_no_diff() -> None:
    from shared.patch_applier import _extract_diff

    assert _extract_diff("just some prose, no diff") == ""


def test_returns_none_for_malformed_json() -> None:
    from shared.patch_applier import _extract_summary

    response = "```json\n{not valid json}\n```"
    assert _extract_summary(response) is None


@pytest.mark.asyncio
async def test_apply_real_diff_to_tmp_repo(tmp_path: Path) -> None:
    """Apply a real diff to a git-initialized tmp repo."""
    # Setup git repo
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    foo = tmp_path / "foo.txt"
    foo.write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    response = """```diff
diff --git a/foo.txt b/foo.txt
--- a/foo.txt
+++ b/foo.txt
@@ -1 +1 @@
-hello
+world
```

```json
{"success": true, "filesChanged": ["foo.txt"], "summary": "say world"}
```
"""

    result = await apply_patch_from_response(tmp_path, response)
    assert "foo.txt" in result.files_changed
    assert foo.read_text() == "world\n"


@pytest.mark.asyncio
async def test_returns_empty_for_no_diff_input(tmp_path: Path) -> None:
    response = "no diff here, just prose"
    result = await apply_patch_from_response(tmp_path, response)
    assert result.files_changed == []
    assert result.applied_diff == ""


@pytest.mark.asyncio
async def test_raises_on_failed_apply(tmp_path: Path) -> None:
    """A diff against a non-existent file should fail to apply."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "real.txt").write_text("real\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    response = """```diff
diff --git a/nonexistent.txt b/nonexistent.txt
--- a/nonexistent.txt
+++ b/nonexistent.txt
@@ -1 +1 @@
-old content that does not exist
+new
```
"""
    with pytest.raises(PatchApplyError):
        await apply_patch_from_response(tmp_path, response)


@pytest.mark.asyncio
async def test_applies_diff_with_inaccurate_hunk_counts(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    target = tmp_path / "math_utils.py"
    target.write_text("def add(a, b):\n    return a - b\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    response = """```diff
diff --git a/math_utils.py b/math_utils.py
index e69de29..f1c6d9e 100644
--- a/math_utils.py
+++ b/math_utils.py
@@ -1,3 +1,3 @@
 def add(a, b):
-    return a - b
+    return a + b
```
```json
{"success": true, "filesChanged": ["math_utils.py"], "summary": "Fix add"}
```
"""

    result = await apply_patch_from_response(tmp_path, response)

    assert result.files_changed == ["math_utils.py"]
    assert target.read_text() == "def add(a, b):\n    return a + b\n"
