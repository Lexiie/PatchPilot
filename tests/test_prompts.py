from pathlib import Path
import subprocess

from shared.models import FailureClassification
from shared.prompts import build_repair_prompt


def test_repair_prompt_includes_small_tracked_file_context(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    source = tmp_path / "app.py"
    source.write_text("def add(a, b):\n    return a - b\n")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)

    classification = FailureClassification(
        type="unit_test",
        confidence=0.85,
        evidence=["AssertionError"],
        likely_files=["app.py"],
        repairability="patch_with_review",
        risk="medium",
    )

    prompt = build_repair_prompt(
        repo_path=str(tmp_path),
        failure_log="assert -1 == 5",
        classification=classification,
        verify_command="pytest",
        budget_usd=0.25,
        max_attempts=3,
        forbidden_paths=[".env*"],
    )

    assert "# Repository context" in prompt
    assert "## app.py" in prompt
    assert "return a - b" in prompt
