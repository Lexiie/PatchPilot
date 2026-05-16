"""Tests for shared.policy — policy loading and enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.models import (
    FailureClassification,
    PolicyConfig,
    PolicyRepairConfig,
)
from shared.policy import (
    check_failure_type_allowed,
    check_forbidden_paths,
    check_requires_review,
    load_policy,
)


@pytest.fixture
def mock_policy() -> PolicyConfig:
    return PolicyConfig(
        repair=PolicyRepairConfig(
            allowed_failure_types=["lint", "format", "typecheck", "unit_test", "dependency_config"],
            forbidden_failure_types=["environment_missing_secret", "network_or_infra"],
            forbidden_paths=[".env*", "secrets/**", "infra/prod/**"],
            require_human_review_for=["auth/**", "migrations/**"],
        )
    )


def test_allows_permitted_failure_types(mock_policy: PolicyConfig) -> None:
    classification = FailureClassification(
        type="lint", confidence=0.9, evidence=[], likely_files=[],
        repairability="safe_auto_patch", risk="low",
    )
    assert check_failure_type_allowed(classification, mock_policy) is None


def test_rejects_forbidden_failure_types(mock_policy: PolicyConfig) -> None:
    classification = FailureClassification(
        type="network_or_infra", confidence=0.8, evidence=[], likely_files=[],
        repairability="do_not_attempt", risk="high",
    )
    result = check_failure_type_allowed(classification, mock_policy)
    assert result is not None
    assert "forbidden" in result


def test_rejects_unlisted_types(mock_policy: PolicyConfig) -> None:
    classification = FailureClassification(
        type="snapshot", confidence=0.8, evidence=[], likely_files=[],
        repairability="safe_auto_patch", risk="low",
    )
    result = check_failure_type_allowed(classification, mock_policy)
    assert result is not None
    assert "not in the allowed list" in result


def test_detects_forbidden_path_violations(mock_policy: PolicyConfig) -> None:
    violations = check_forbidden_paths([".env.local", "src/app.ts"], mock_policy)
    assert len(violations) == 1
    assert ".env" in violations[0]


def test_detects_files_requiring_review(mock_policy: PolicyConfig) -> None:
    files = check_requires_review(["auth/login.ts", "src/app.ts"], mock_policy)
    assert "auth/login.ts" in files
    assert "src/app.ts" not in files


def test_load_policy_returns_defaults_if_missing(tmp_path: Path) -> None:
    policy = load_policy(tmp_path)
    assert policy.version == 2
    assert "lint" in policy.repair.allowed_failure_types


def test_load_policy_parses_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / ".patchpilot.yml"
    config_path.write_text(
        """version: 2
repair:
  max_attempts: 5
  allowed_failure_types: [lint]
  forbidden_failure_types: []
  forbidden_paths: ["secrets/**"]
  require_human_review_for: []
verification:
  required_commands: ["npm test"]
"""
    )
    policy = load_policy(tmp_path)
    assert policy.repair.max_attempts == 5
    assert policy.repair.allowed_failure_types == ["lint"]
    assert "npm test" in policy.verification.required_commands
