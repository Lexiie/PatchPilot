"""Failure classifier — pattern-based with confidence scoring.

This is a Python port of the v1 TypeScript classifier, expanded to cover
multiple language ecosystems: TS/JS, Python, Go, Rust, Java, Ruby, and
common generic patterns. Same 13 failure categories, same risk/repairability
mapping.

Classification is deterministic and runs without an LLM. Triage agent will
fall back to app.ai() only when confidence < 0.4 (i.e., classifier returned
'unknown' or low-confidence guess).

Each rule defines (failure_type, regex_patterns, repairability, risk).
Patterns are ordered by specificity within each rule; rules are ordered
by specificity across the whole file (more specific types first).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from shared.models import FailureClassification, FailureType, Repairability, Risk


@dataclass(frozen=True)
class ClassificationRule:
    """A pattern set for one failure type."""

    type: FailureType
    patterns: tuple[re.Pattern[str], ...]
    repairability: Repairability
    risk: Risk


# Ordered by specificity — more specific patterns first
RULES: tuple[ClassificationRule, ...] = (
    # ─── lint ─────────────────────────────────────────────────────────
    ClassificationRule(
        type="lint",
        patterns=(
            # JS / TS
            re.compile(r"eslint", re.IGNORECASE),
            re.compile(r"\blint\b", re.IGNORECASE),
            re.compile(r"prettier.*error", re.IGNORECASE),
            re.compile(r"warning.*no-unused", re.IGNORECASE),
            # Python
            re.compile(r"\bruff\b", re.IGNORECASE),
            re.compile(r"\bflake8\b", re.IGNORECASE),
            re.compile(r"\bpylint\b", re.IGNORECASE),
            re.compile(r"\bE\d{3,4}\b\s+line too long", re.IGNORECASE),  # ruff/flake8
            re.compile(r"\bF\d{3,4}\b\s+", re.IGNORECASE),  # flake8 codes
            # Go
            re.compile(r"golangci-lint", re.IGNORECASE),
            re.compile(r"go\s+vet", re.IGNORECASE),
            # Ruby
            re.compile(r"\brubocop\b", re.IGNORECASE),
            # Generic
            re.compile(r"\d+\s+lint\s+(?:error|warning)", re.IGNORECASE),
        ),
        repairability="safe_auto_patch",
        risk="low",
    ),
    # ─── format ──────────────────────────────────────────────────────
    ClassificationRule(
        type="format",
        patterns=(
            # JS / TS
            re.compile(r"prettier", re.IGNORECASE),
            re.compile(r"formatting", re.IGNORECASE),
            re.compile(r"code style", re.IGNORECASE),
            # Python
            re.compile(r"\bblack\b.*would reformat", re.IGNORECASE),
            re.compile(r"would reformat\s+\S+\.py", re.IGNORECASE),
            re.compile(r"\bisort\b", re.IGNORECASE),
            re.compile(r"\bautopep8\b", re.IGNORECASE),
            # Go
            re.compile(r"\bgofmt\b", re.IGNORECASE),
            re.compile(r"\bgoimports\b", re.IGNORECASE),
            # Rust
            re.compile(r"\brustfmt\b", re.IGNORECASE),
            # Generic
            re.compile(r"\bformat check\b", re.IGNORECASE),
        ),
        repairability="safe_auto_patch",
        risk="low",
    ),
    # ─── typecheck ───────────────────────────────────────────────────
    ClassificationRule(
        type="typecheck",
        patterns=(
            # TypeScript
            re.compile(r"\bTS\d{4}\b"),
            re.compile(r"\btsc\b.*error", re.IGNORECASE),
            re.compile(r"\btypescript\b.*error", re.IGNORECASE),
            re.compile(r"type error", re.IGNORECASE),
            re.compile(r"is not assignable to type", re.IGNORECASE),
            # Python
            re.compile(r"\bmypy\b", re.IGNORECASE),
            re.compile(r"\berror:.*incompatible type", re.IGNORECASE),
            re.compile(r"\bpyright\b", re.IGNORECASE),
            # Rust — type-related compile errors (E0308 = mismatched types, etc.)
            re.compile(r"error\[E0?308\]", re.IGNORECASE),
            re.compile(r"error\[E0?277\]", re.IGNORECASE),
            re.compile(r"\bmismatched types\b", re.IGNORECASE),
            # Java
            re.compile(r"incompatible types", re.IGNORECASE),
            re.compile(r"cannot resolve symbol", re.IGNORECASE),
            # Go
            re.compile(r"cannot use .* as .* in", re.IGNORECASE),
            re.compile(r"undefined: \w+", re.IGNORECASE),
        ),
        repairability="safe_auto_patch",
        risk="low",
    ),
    # ─── unit_test ───────────────────────────────────────────────────
    ClassificationRule(
        type="unit_test",
        patterns=(
            # JS / TS
            re.compile(r"FAIL.*\.test\.", re.IGNORECASE),
            re.compile(r"\bvitest\b", re.IGNORECASE),
            re.compile(r"\bjest\b", re.IGNORECASE),
            re.compile(r"\bmocha\b", re.IGNORECASE),
            re.compile(r"expect\(.*\)\.to", re.IGNORECASE),
            # Python
            re.compile(r"\bpytest\b", re.IGNORECASE),
            re.compile(r"FAILED\s+\S+\.py::", re.IGNORECASE),
            re.compile(r"AssertionError", re.IGNORECASE),
            re.compile(r"\bunittest\b", re.IGNORECASE),
            # Go
            re.compile(r"---\s+FAIL:\s+Test\w+"),
            re.compile(r"^FAIL\s+\S+\s+\d+\.\d+s", re.MULTILINE),
            # Rust
            re.compile(r"test\s+\S+\s+\.\.\.\s+FAILED"),
            re.compile(r"thread\s+'\w+'\s+panicked"),
            # Ruby
            re.compile(r"\brspec\b", re.IGNORECASE),
            re.compile(r"Failure/Error:", re.IGNORECASE),
            re.compile(r"\bminitest\b", re.IGNORECASE),
            # Java
            re.compile(r"\bjunit\b.*FAILED", re.IGNORECASE),
            re.compile(r"Tests run:\s+\d+,.*Failures:\s+[1-9]"),
            # Generic
            re.compile(r"\btest.*failed", re.IGNORECASE),
        ),
        repairability="patch_with_review",
        risk="medium",
    ),
    # ─── integration_test ───────────────────────────────────────────
    ClassificationRule(
        type="integration_test",
        patterns=(
            re.compile(r"integration.*fail", re.IGNORECASE),
            re.compile(r"e2e.*fail", re.IGNORECASE),
            re.compile(r"\bcypress\b", re.IGNORECASE),
            re.compile(r"\bplaywright\b.*error", re.IGNORECASE),
            re.compile(r"\bselenium\b.*error", re.IGNORECASE),
            re.compile(r"\bcapybara\b", re.IGNORECASE),
        ),
        repairability="patch_with_review",
        risk="medium",
    ),
    # ─── dependency_config ──────────────────────────────────────────
    ClassificationRule(
        type="dependency_config",
        patterns=(
            # JS / TS
            re.compile(r"cannot find module", re.IGNORECASE),
            re.compile(r"module not found", re.IGNORECASE),
            re.compile(r"peer dep", re.IGNORECASE),
            re.compile(r"\bERESOLVE\b"),
            # Python
            re.compile(r"ModuleNotFoundError", re.IGNORECASE),
            re.compile(r"No module named '\w+'", re.IGNORECASE),
            re.compile(r"\bImportError\b", re.IGNORECASE),
            # Go
            re.compile(r"package\s+\S+\s+is not in std", re.IGNORECASE),
            re.compile(r"go:\s+module\s+\S+\s+not found", re.IGNORECASE),
            re.compile(r"missing go\.sum entry", re.IGNORECASE),
            # Rust
            re.compile(r"can't find crate for `\w+`", re.IGNORECASE),
            re.compile(r"unresolved import", re.IGNORECASE),
            # Java
            re.compile(r"cannot find symbol.*class", re.IGNORECASE),
            re.compile(r"package\s+\S+\s+does not exist", re.IGNORECASE),
            # Ruby
            re.compile(r"cannot load such file", re.IGNORECASE),
            re.compile(r"\bgem.*not found", re.IGNORECASE),
        ),
        repairability="patch_with_review",
        risk="low",
    ),
    # ─── package_lock ───────────────────────────────────────────────
    ClassificationRule(
        type="package_lock",
        patterns=(
            # JS / TS
            re.compile(r"\blockfile\b", re.IGNORECASE),
            re.compile(r"package-lock"),
            re.compile(r"yarn\.lock"),
            re.compile(r"pnpm-lock"),
            # Python
            re.compile(r"poetry\.lock"),
            re.compile(r"\buv\.lock\b"),
            re.compile(r"Pipfile\.lock"),
            # Go
            re.compile(r"go\.sum", re.IGNORECASE),
            # Rust
            re.compile(r"Cargo\.lock"),
            # Ruby
            re.compile(r"Gemfile\.lock"),
        ),
        repairability="safe_auto_patch",
        risk="low",
    ),
    # ─── snapshot ───────────────────────────────────────────────────
    ClassificationRule(
        type="snapshot",
        patterns=(
            re.compile(r"snapshot.*obsolete", re.IGNORECASE),
            re.compile(r"snapshot.*mismatch", re.IGNORECASE),
            re.compile(r"toMatchSnapshot", re.IGNORECASE),
            re.compile(r"snapshot does not match", re.IGNORECASE),
            # Python: pytest-snapshot, syrupy
            re.compile(r"\bsyrupy\b.*mismatch", re.IGNORECASE),
            re.compile(r"snapshot\s+\S+\.ambr", re.IGNORECASE),
            # Rust: insta
            re.compile(r"\binsta\b.*snapshot", re.IGNORECASE),
        ),
        repairability="safe_auto_patch",
        risk="low",
    ),
    # ─── build_compile ──────────────────────────────────────────────
    ClassificationRule(
        type="build_compile",
        patterns=(
            # Generic
            re.compile(r"build.*fail", re.IGNORECASE),
            re.compile(r"compilation.*error", re.IGNORECASE),
            re.compile(r"SyntaxError", re.IGNORECASE),
            re.compile(r"cannot compile", re.IGNORECASE),
            # Rust — error[Exxxx] but not the typecheck-specific ones above
            re.compile(r"error\[E0?(?!308|277)\d{3,4}\]"),
            re.compile(r"could not compile"),
            # Java — broader compile errors
            re.compile(r"cannot find symbol(?!.*class)", re.IGNORECASE),
            re.compile(r"\[ERROR\].*\.java", re.IGNORECASE),
            # Go — build errors
            re.compile(r"^\S+\.go:\d+:\d+:\s+", re.MULTILINE),
            # C / C++
            re.compile(r"undefined reference to", re.IGNORECASE),
            re.compile(r"fatal error:.*\.h"),
        ),
        repairability="patch_with_review",
        risk="medium",
    ),
    # ─── environment_missing_secret ────────────────────────────────
    ClassificationRule(
        type="environment_missing_secret",
        patterns=(
            re.compile(r"secret.*not set", re.IGNORECASE),
            re.compile(r"env.*missing", re.IGNORECASE),
            re.compile(r"\bundefined.*API_KEY\b", re.IGNORECASE),
            re.compile(r"ENOENT.*\.env", re.IGNORECASE),
            re.compile(r"environment variable\s+\w+\s+(?:not set|required)", re.IGNORECASE),
            re.compile(r"missing.*credential", re.IGNORECASE),
        ),
        repairability="do_not_attempt",
        risk="high",
    ),
    # ─── network_or_infra ──────────────────────────────────────────
    ClassificationRule(
        type="network_or_infra",
        patterns=(
            re.compile(r"\bETIMEDOUT\b"),
            re.compile(r"\bECONNREFUSED\b"),
            re.compile(r"rate limit", re.IGNORECASE),
            re.compile(r"\b503\b"),
            re.compile(r"\b502\b"),
            re.compile(r"\b504\b"),
            re.compile(r"network.*error", re.IGNORECASE),
            re.compile(r"\btimeout\b", re.IGNORECASE),
            # Python urllib/requests
            re.compile(r"ConnectionError", re.IGNORECASE),
            re.compile(r"ConnectionRefusedError", re.IGNORECASE),
            # Go
            re.compile(r"connection refused", re.IGNORECASE),
            re.compile(r"i/o timeout", re.IGNORECASE),
        ),
        repairability="do_not_attempt",
        risk="high",
    ),
    # ─── flaky_test ────────────────────────────────────────────────
    ClassificationRule(
        type="flaky_test",
        patterns=(
            re.compile(r"\bflaky\b", re.IGNORECASE),
            re.compile(r"intermittent", re.IGNORECASE),
            re.compile(r"retry.*failed", re.IGNORECASE),
            re.compile(r"@pytest\.mark\.flaky", re.IGNORECASE),
        ),
        repairability="triage_only",
        risk="medium",
    ),
)

# File path patterns commonly seen in stack traces / error reports.
# Covers many language layouts: src/, lib/, test/, app/, packages/, tests/,
# pkg/ (Go), cmd/ (Go), internal/ (Go), crates/ (Rust workspaces).
_FILE_PATH_PATTERN = re.compile(
    r"(?:^|\s|\()((?:src|lib|test|app|packages|tests?|pkg|cmd|internal|crates)/"
    r"[\w\-./]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|java|kt|rb|c|cc|cpp|h|hpp))",
    re.MULTILINE,
)


def classify_failure(log_content: str) -> FailureClassification:
    """Classify a failure log into one of 13 categories.

    Uses pattern matching across all rules. Score per rule is the count of
    pattern matches. The rule with the highest score wins. Confidence is
    capped at 0.95 and grows with match count.

    If no rule matches, returns 'unknown' with confidence 0.1.

    Supported language ecosystems (varying coverage):
        - TypeScript / JavaScript (most extensive)
        - Python (pytest, ruff, black, mypy, ModuleNotFoundError)
        - Go (go test, go mod, gofmt, golangci-lint)
        - Rust (cargo, rustfmt, error[Exxxx])
        - Java (javac, junit, maven [ERROR])
        - Ruby (rspec, rubocop, bundler)
        - C/C++ (basic compile error patterns)

    Args:
        log_content: Normalized failure log text. Should already be
            redacted via shared.redactor.redact_secrets() if it might
            contain secrets.

    Returns:
        FailureClassification with type, confidence, evidence, files,
        repairability, and risk populated.
    """
    scores: list[tuple[ClassificationRule, int, list[str]]] = []

    for rule in RULES:
        match_count = 0
        evidence: list[str] = []
        for pattern in rule.patterns:
            matches = pattern.findall(log_content)
            if matches:
                match_count += len(matches)
                # Collect a sample of evidence (one per pattern is enough)
                first = matches[0]
                if isinstance(first, str):
                    evidence.append(first)
        if match_count > 0:
            scores.append((rule, match_count, evidence))

    if not scores:
        return FailureClassification(
            type="unknown",
            confidence=0.1,
            evidence=[],
            likely_files=_extract_file_paths(log_content),
            repairability="triage_only",
            risk="medium",
        )

    # Highest match count wins
    scores.sort(key=lambda s: s[1], reverse=True)
    best_rule, best_count, best_evidence = scores[0]

    # Confidence: 0.5 base + 0.1 per match, capped at 0.95
    confidence = min(0.95, 0.5 + best_count * 0.1)

    return FailureClassification(
        type=best_rule.type,
        confidence=confidence,
        evidence=best_evidence[:5],
        likely_files=_extract_file_paths(log_content),
        repairability=best_rule.repairability,
        risk=best_rule.risk,
    )


def _extract_file_paths(log_content: str) -> list[str]:
    """Extract likely file paths from log content.

    Looks for paths in common project layouts (src/, pkg/, crates/, etc.)
    followed by a known source file extension. Returns up to 10 unique matches.
    """
    matches = _FILE_PATH_PATTERN.findall(log_content)
    seen: list[str] = []
    for match in matches:
        if match not in seen:
            seen.append(match)
        if len(seen) >= 10:
            break
    return seen
