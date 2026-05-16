"""Tests for shared.redactor — secret redaction and log normalization."""

from __future__ import annotations

from shared.redactor import normalize_logs, redact_secrets

# Build fixtures at runtime so GitHub secret scanner doesn't flag the file
_FAKE_GH_TOKEN = "ghp_" + "X" * 36
_FAKE_AWS = "AKIA" + "IOSFODNN7" + "EXAMPLE"
_FAKE_STRIPE = "sk_" + "live_" + "abcdefghijklmnopqrstuvwx"


def test_redacts_github_token() -> None:
    text = f"Authorization: token {_FAKE_GH_TOKEN}"
    result = redact_secrets(text)
    assert "ghp_" not in result.redacted_text
    assert result.count > 0


def test_redacts_aws_access_key() -> None:
    text = f"AWS_ACCESS_KEY_ID={_FAKE_AWS}"
    result = redact_secrets(text)
    assert _FAKE_AWS not in result.redacted_text


def test_redacts_stripe_keys() -> None:
    text = f"stripe_key: {_FAKE_STRIPE}"
    result = redact_secrets(text)
    assert "sk_live_" not in result.redacted_text


def test_preserves_non_secret_text() -> None:
    text = "Running npm test...\nAll 5 tests passed."
    result = redact_secrets(text)
    assert result.redacted_text == text
    assert result.count == 0


def test_redacts_database_url() -> None:
    text = "Error connecting to postgres://user:pass@host:5432/db"
    result = redact_secrets(text)
    assert "postgres://" not in result.redacted_text


def test_normalize_logs_short_unchanged() -> None:
    log = "line 1\nline 2\nline 3"
    assert normalize_logs(log) == log


def test_normalize_logs_truncates_long_logs_around_errors() -> None:
    lines = [f"line {i}" for i in range(500)]
    lines[250] = "ERROR: something failed here"
    log = "\n".join(lines)
    result = normalize_logs(log, max_lines=50)
    assert "ERROR: something failed here" in result
    assert len(result.split("\n")) <= 50


def test_normalize_logs_falls_back_to_tail() -> None:
    lines = [f"info: step {i}" for i in range(500)]
    log = "\n".join(lines)
    result = normalize_logs(log, max_lines=50)
    assert len(result.split("\n")) == 50
    assert "step 499" in result
