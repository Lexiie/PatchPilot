"""Tests for shared.classifier — pattern-based failure classification.

These mirror the v1 TypeScript tests, ported to pytest. Same failure
categories, same expected outputs.
"""

from __future__ import annotations

import pytest

from shared.classifier import classify_failure


def test_classifies_eslint_as_lint() -> None:
    log = "ESLint found 3 errors in src/index.ts\n  error  no-unused-vars"
    result = classify_failure(log)
    assert result.type == "lint"
    assert result.repairability == "safe_auto_patch"
    assert result.risk == "low"
    assert result.confidence > 0.5


def test_classifies_typescript_as_typecheck() -> None:
    log = "src/app.ts(10,5): error TS2322: Type 'string' is not assignable to type 'number'."
    result = classify_failure(log)
    assert result.type == "typecheck"
    assert result.repairability == "safe_auto_patch"


def test_classifies_unit_test_failure() -> None:
    log = (
        "FAIL src/utils.test.ts\n"
        "  ● add › should sum two numbers\n"
        "    expect(received).toBe(expected)\n"
        "    AssertionError: expected 3 to be 4"
    )
    result = classify_failure(log)
    assert result.type == "unit_test"
    assert result.repairability == "patch_with_review"


def test_classifies_network_failure() -> None:
    log = "Error: connect ETIMEDOUT 10.0.0.1:443\n  at TCPConnectWrap.afterConnect"
    result = classify_failure(log)
    assert result.type == "network_or_infra"
    assert result.repairability == "do_not_attempt"


def test_returns_unknown_for_unrecognized_logs() -> None:
    log = "something completely unrelated happened here"
    result = classify_failure(log)
    assert result.type == "unknown"
    assert result.repairability == "triage_only"


def test_extracts_file_paths() -> None:
    log = (
        "Error in src/parser.ts:42\n"
        "  Cannot read property of undefined\n"
        "  at src/utils/helper.ts:10"
    )
    result = classify_failure(log)
    assert "src/parser.ts" in result.likely_files
    assert "src/utils/helper.ts" in result.likely_files


def test_classifies_dependency_config() -> None:
    log = "Error: Cannot find module 'lodash' from src/index.js"
    result = classify_failure(log)
    assert result.type == "dependency_config"


def test_classifies_secret_missing() -> None:
    log = "Error: env GITHUB_TOKEN missing"
    result = classify_failure(log)
    assert result.type == "environment_missing_secret"
    assert result.risk == "high"
    assert result.repairability == "do_not_attempt"


def test_python_test_failure() -> None:
    log = (
        "FAILED tests/test_app.py::test_calculate - AssertionError: expected 5 got 4\n"
        "pytest collected 12 items, 1 failed"
    )
    result = classify_failure(log)
    assert result.type == "unit_test"


@pytest.mark.parametrize(
    ("log", "expected_type"),
    [
        ("prettier check failed: code style violations", "format"),
        ("snapshot mismatch in toMatchSnapshot", "snapshot"),
        ("compilation error: SyntaxError on line 5", "build_compile"),
        ("flaky test detected: retry failed twice", "flaky_test"),
    ],
)
def test_classify_parametrized(log: str, expected_type: str) -> None:
    assert classify_failure(log).type == expected_type
