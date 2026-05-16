"""Policy loader and enforcement.

Loads .patchpilot.yml from a repo and provides predicates used by
triage-agent (failure type allowed?) and verify-agent (path allowed?).

Policy is enforced at the AgentField agent level. AgentField's own
policy gates (DID-based ALLOW/DENY) provide an additional layer at the
control plane, but PatchPilot's per-repo policy lives in the YAML file
that ships with the repo being repaired.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from shared.models import FailureClassification, PolicyConfig


def load_policy(repo_path: str | Path) -> PolicyConfig:
    """Load .patchpilot.yml from a repo, or return defaults if absent.

    Args:
        repo_path: Path to the repo root.

    Returns:
        Validated PolicyConfig. Falls back to defaults if file is missing
        or empty.

    Raises:
        pydantic.ValidationError: If the YAML is malformed against the schema.
    """
    config_path = Path(repo_path) / ".patchpilot.yml"
    if not config_path.exists():
        return PolicyConfig()

    raw = config_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw) or {}
    return PolicyConfig.model_validate(parsed)


def check_failure_type_allowed(
    classification: FailureClassification,
    policy: PolicyConfig,
) -> str | None:
    """Check if a classified failure type is allowed by policy.

    Args:
        classification: The failure classification from triage-agent.
        policy: Active policy config.

    Returns:
        None if allowed. A human-readable rejection reason if forbidden.
    """
    if classification.type in policy.repair.forbidden_failure_types:
        return f"Failure type '{classification.type}' is forbidden by policy"
    if classification.type not in policy.repair.allowed_failure_types:
        return f"Failure type '{classification.type}' is not in the allowed list"
    return None


def check_forbidden_paths(
    changed_files: list[str],
    policy: PolicyConfig,
) -> list[str]:
    """Find any changed files that violate the forbidden_paths policy.

    Args:
        changed_files: Paths of files modified by the repair.
        policy: Active policy config.

    Returns:
        List of human-readable violation descriptions. Empty if all OK.
    """
    violations: list[str] = []
    for file in changed_files:
        for pattern in policy.repair.forbidden_paths:
            if _glob_match(file, pattern):
                violations.append(f"File '{file}' matches forbidden pattern '{pattern}'")
    return violations


def check_requires_review(
    changed_files: list[str],
    policy: PolicyConfig,
) -> list[str]:
    """Find changed files that require human review per policy.

    Args:
        changed_files: Paths of files modified by the repair.
        policy: Active policy config.

    Returns:
        List of file paths that need human review (subset of changed_files).
    """
    review_required: list[str] = []
    for file in changed_files:
        for pattern in policy.repair.require_human_review_for:
            if _glob_match(file, pattern):
                review_required.append(file)
                break
    return review_required


def _glob_match(file_path: str, pattern: str) -> bool:
    """Simple glob matcher supporting * and **.

    Implements just enough of glob semantics for policy patterns:
    - `*` matches anything except `/`
    - `**` matches anything including `/`
    - `?` and character classes are not supported

    Args:
        file_path: File path to test.
        pattern: Glob pattern.

    Returns:
        True if path matches pattern.
    """
    regex = (
        re.escape(pattern)
        .replace(r"\*\*", "{{GLOBSTAR}}")
        .replace(r"\*", "[^/]*")
        .replace("{{GLOBSTAR}}", ".*")
    )
    return re.match(f"^{regex}$", file_path) is not None
