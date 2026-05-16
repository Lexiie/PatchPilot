"""Secret redaction for failure logs.

Strips common secret patterns before any model call. Applied at the
triage-agent layer, before app.ai() ever sees the log content.

Patterns are conservative — false positives (e.g. high-entropy strings
that look like tokens but aren't) are acceptable; false negatives are
not. Better to redact too much than leak a credential.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from shared.models import RedactionResult


@dataclass(frozen=True)
class _SecretPattern:
    name: str
    regex: re.Pattern[str]


_PATTERNS: tuple[_SecretPattern, ...] = (
    _SecretPattern("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}")),
    _SecretPattern("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    _SecretPattern(
        "AWS Secret Key",
        re.compile(
            r"aws_secret_access_key\s*=\s*([A-Za-z0-9/+=]{40})",
            re.IGNORECASE,
        ),
    ),
    _SecretPattern("npm Token", re.compile(r"npm_[A-Za-z0-9]{36}")),
    _SecretPattern(
        "Generic Token",
        re.compile(
            r"(?:token|secret|password|api_key)\s*[:=]\s*['\"]?[A-Za-z0-9\-._~+/]{20,}['\"]?",
            re.IGNORECASE,
        ),
    ),
    _SecretPattern(
        "Private Key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----[\s\S]*?"
            r"-----END (?:RSA |EC |DSA )?PRIVATE KEY-----"
        ),
    ),
    _SecretPattern("Stripe Key", re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{24,}")),
    _SecretPattern(
        "Database URL",
        re.compile(r"(?:postgres|mysql|mongodb)://[^\s'\"]+", re.IGNORECASE),
    ),
    _SecretPattern("High Entropy", re.compile(r"[A-Za-z0-9+/=]{40,}")),
)


def redact_secrets(text: str) -> RedactionResult:
    """Replace common secret patterns with [REDACTED:<pattern-name>] tags.

    Patterns are applied in order. Earlier (more specific) patterns redact
    first, so a GitHub token will be tagged as 'GitHub Token' rather than
    fall through to the catch-all 'High Entropy' pattern.

    Args:
        text: Raw log text that may contain secrets.

    Returns:
        RedactionResult with sanitized text, count of redactions, and
        list of pattern names that matched (for audit visibility).
    """
    redacted = text
    count = 0
    matched_patterns: list[str] = []

    for pattern in _PATTERNS:
        matches = pattern.regex.findall(redacted)
        if matches:
            count += len(matches)
            matched_patterns.append(pattern.name)
            redacted = pattern.regex.sub(f"[REDACTED:{pattern.name}]", redacted)

    return RedactionResult(
        redacted_text=redacted,
        count=count,
        matched_patterns=matched_patterns,
    )


def normalize_logs(raw_log: str, max_lines: int = 200) -> str:
    """Truncate long logs to keep most relevant failure context.

    If the log is already shorter than max_lines, returns unchanged.
    Otherwise, looks for error markers and keeps a window of context
    around each. Falls back to last N lines if no error markers found.

    Args:
        raw_log: The full raw log text.
        max_lines: Maximum number of lines to retain.

    Returns:
        Normalized log string, ≤ max_lines lines.
    """
    lines = raw_log.split("\n")
    if len(lines) <= max_lines:
        return raw_log

    error_indicators = ("error", "Error", "ERROR", "FAIL", "failed", "FAILED", "✗", "✖", "×")
    relevant: list[int] = [
        i for i, line in enumerate(lines) if any(ind in line for ind in error_indicators)
    ]

    if not relevant:
        # No error markers — just keep tail
        return "\n".join(lines[-max_lines:])

    context_radius = 5
    selected: set[int] = set()
    for idx in relevant:
        for i in range(max(0, idx - context_radius), min(len(lines), idx + context_radius + 1)):
            selected.add(i)

    sorted_indices = sorted(selected)
    result: list[str] = []
    last_idx = -1
    for idx in sorted_indices:
        if last_idx != -1 and idx - last_idx > 1:
            result.append("... (truncated) ...")
        result.append(lines[idx])
        last_idx = idx

    return "\n".join(result[:max_lines])
