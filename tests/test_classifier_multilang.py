"""Multi-language classifier coverage tests.

Verifies that PatchPilot's pattern-based classifier handles failure
formats from multiple language ecosystems, not just TypeScript/JavaScript.

Coverage matrix:
    Python:     ruff, black, mypy, pytest, ModuleNotFoundError, flake8
    Go:         go test, go vet, go mod, gofmt, undefined symbol
    Rust:       error[Exxxx], cargo test panic, rustfmt
    Java:       javac errors, junit, maven [ERROR]
    Ruby:       rspec, rubocop
    C/C++:      undefined reference

If any of these regress to 'unknown', the classifier broke. Add new
samples here when expanding language support.
"""

from __future__ import annotations

import pytest

from shared.classifier import classify_failure


# ─── Python ─────────────────────────────────────────────────────────


def test_python_ruff_lint() -> None:
    log = "src/app.py:5:1: E501 line too long (88 > 79 characters)"
    assert classify_failure(log).type == "lint"


def test_python_black_format() -> None:
    log = "would reformat src/app.py"
    assert classify_failure(log).type == "format"


def test_python_mypy_typecheck() -> None:
    log = "src/app.py:10: error: Incompatible types in assignment"
    assert classify_failure(log).type == "typecheck"


def test_python_pytest_unit_test() -> None:
    log = "FAILED tests/test_app.py::test_calculate - AssertionError"
    assert classify_failure(log).type == "unit_test"


def test_python_module_not_found() -> None:
    log = "ModuleNotFoundError: No module named 'requests'"
    assert classify_failure(log).type == "dependency_config"


def test_python_flake8_unused() -> None:
    log = "src/foo.py:1:1: F401 unused import"
    assert classify_failure(log).type == "lint"


# ─── Go ─────────────────────────────────────────────────────────────


def test_go_test_failure() -> None:
    log = (
        "--- FAIL: TestAdd (0.00s)\n"
        "    main_test.go:12: expected 4, got 3\n"
        "FAIL    example.com/foo  0.123s"
    )
    assert classify_failure(log).type == "unit_test"


def test_go_vet() -> None:
    log = "go vet: src/main.go:5:2: missing return at end of function"
    assert classify_failure(log).type == "lint"


def test_go_mod_missing() -> None:
    log = "main.go:3:8: package github.com/foo/bar is not in std"
    assert classify_failure(log).type == "dependency_config"


def test_go_gofmt() -> None:
    log = "gofmt would format src/main.go"
    assert classify_failure(log).type == "format"


def test_go_undefined_symbol() -> None:
    log = "undefined: SomeFunc"
    assert classify_failure(log).type == "typecheck"


# ─── Rust ───────────────────────────────────────────────────────────


def test_rust_mismatched_types() -> None:
    log = "error[E0308]: mismatched types\n  --> src/main.rs:5:13"
    assert classify_failure(log).type == "typecheck"


def test_rust_test_panic() -> None:
    log = (
        "test test_add ... FAILED\n"
        'thread "test_add" panicked at src/lib.rs:10'
    )
    assert classify_failure(log).type == "unit_test"


def test_rust_unresolved_import() -> None:
    log = 'error[E0432]: unresolved import "std::collections::HashMap"'
    assert classify_failure(log).type == "dependency_config"


def test_rust_rustfmt() -> None:
    log = "rustfmt would change src/main.rs"
    assert classify_failure(log).type == "format"


# ─── Java ───────────────────────────────────────────────────────────


def test_java_cannot_find_symbol_class() -> None:
    log = "[ERROR] /src/Main.java:[10,5] cannot find symbol class Foo"
    assert classify_failure(log).type == "dependency_config"


def test_java_incompatible_types() -> None:
    log = "[ERROR] /src/App.java:[5,1] incompatible types: String cannot be converted to int"
    assert classify_failure(log).type == "typecheck"


def test_java_junit_failures() -> None:
    log = "Tests run: 12, Failures: 1, Errors: 0, Skipped: 0"
    assert classify_failure(log).type == "unit_test"


# ─── Ruby ───────────────────────────────────────────────────────────


def test_ruby_rspec() -> None:
    log = (
        "Failure/Error: expect(result).to eq(4)\n"
        "  expected: 4\n"
        "       got: 3"
    )
    assert classify_failure(log).type == "unit_test"


def test_ruby_rubocop() -> None:
    log = "rubocop offense: src/app.rb:5:3 Style/StringLiterals"
    assert classify_failure(log).type == "lint"


# ─── C/C++ ─────────────────────────────────────────────────────────


def test_c_undefined_reference() -> None:
    log = 'main.c: undefined reference to "foo"'
    assert classify_failure(log).type == "build_compile"


# ─── Aggregate coverage check ──────────────────────────────────────


@pytest.mark.parametrize(
    ("language", "log", "expected_type"),
    [
        ("python", "ruff src/app.py:5:1: E501 line too long", "lint"),
        ("python", "FAILED tests/test_x.py::test_y - AssertionError", "unit_test"),
        ("go", "--- FAIL: TestSomething (0.01s)", "unit_test"),
        ("rust", "error[E0308]: mismatched types", "typecheck"),
        ("java", "Tests run: 5, Failures: 2", "unit_test"),
        ("ruby", "Failure/Error: expect(x).to be_truthy", "unit_test"),
    ],
)
def test_multi_language_smoke(language: str, log: str, expected_type: str) -> None:
    """Smoke test: each language should have at least one classifiable failure."""
    result = classify_failure(log)
    assert result.type == expected_type, f"{language}: expected {expected_type}, got {result.type}"


def test_garbage_text_returns_unknown() -> None:
    """Sanity: non-failure text should still be 'unknown'."""
    log = "Hello world, this is a happy log message"
    assert classify_failure(log).type == "unknown"
