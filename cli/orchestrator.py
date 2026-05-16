"""Run orchestrator — chains triage → repair → verify → audit.

This module contains the synchronous orchestration logic used by the CLI
and the webhook handler. Each step emits per-call cost data that ends up
in the cost ledger via audit-agent.

In production (with AgentField running), each step is `app.call()` to
the corresponding agent. In standalone mode (no control plane), the
shared modules are called directly so the CLI can run without Docker.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared import classifier, policy as policy_mod, redactor
from shared.github import create_pull_request, fetch_run_logs, resolve_latest_failed_run
from shared.models import (
    CostLedger,
    FailureClassification,
    GitHubRunSource,
    LedgerStep,
    LedgerTotals,
    LocalCommandSource,
    PatchPilotMode,
    PatchPilotRun,
    RepairResult,
    RunArtifacts,
    VerificationResult,
)


@dataclass
class RepairOptions:
    repo: str
    command: str
    budget: float = 0.50
    mode: PatchPilotMode = "full"
    dry_run: bool = False


@dataclass
class GitHubRepairOptions:
    repo: str
    run: str = "latest-failed"
    budget: float = 0.50
    create_pr: bool = False
    local_path: str = "."
    verify_command: str = "npm test"
    mode: PatchPilotMode = "full"
    dry_run: bool = False


def _generate_run_id() -> str:
    """Generate a PatchPilot-prefixed run identifier."""
    return f"pp_{uuid.uuid4().hex[:12]}"


def _get_run_dir(repo_path: Path, run_id: str) -> Path:
    """Return (and create) the artifacts directory for a run."""
    run_dir = repo_path / ".patchpilot" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


async def _capture_local_command(
    repo_path: Path, command: str, run_dir: Path
) -> LocalCommandSource | None:
    """Run a command and capture stdout/stderr. Returns None if it succeeds."""
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"

    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(repo_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    stdout_path.write_bytes(stdout or b"")
    stderr_path.write_bytes(stderr or b"")

    if proc.returncode == 0:
        return None

    return LocalCommandSource(
        command=command,
        exit_code=proc.returncode or 1,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )


async def repair_local(options: RepairOptions) -> PatchPilotRun:
    """Run the full pipeline against a local repo + failing command."""
    repo_path = Path(options.repo).resolve()
    run_id = _generate_run_id()
    run_dir = _get_run_dir(repo_path, run_id)

    # Step 1: capture failure
    source = await _capture_local_command(repo_path, options.command, run_dir)
    if source is None:
        # Command passed, nothing to repair
        empty_source = LocalCommandSource(
            command=options.command, exit_code=0,
            stdout_path=str(run_dir / "stdout.log"),
            stderr_path=str(run_dir / "stderr.log"),
        )
        return _make_empty_run(run_id, "local", repo_path, empty_source, options.budget)

    # Step 2: run the pipeline
    return await _run_pipeline(
        run_id=run_id,
        repo_path=repo_path,
        run_dir=run_dir,
        source=source,
        log_path=Path(source.stderr_path),  # combined will be built below
        verify_command=options.command,
        budget_usd=options.budget,
        mode=options.mode,
        dry_run=options.dry_run,
        repo_slug=None,
        mode_label="local",
    )


async def repair_github(options: GitHubRepairOptions) -> PatchPilotRun:
    """Run the full pipeline against a failed GitHub Actions run."""
    repo_path = Path(options.local_path).resolve()
    run_id = _generate_run_id()
    run_dir = _get_run_dir(repo_path, run_id)

    # Resolve run ID
    gh_run_id = (
        await resolve_latest_failed_run(options.repo)
        if options.run == "latest-failed"
        else options.run
    )

    # Fetch logs
    log_path = run_dir / "gh-run.log"
    fetched = await fetch_run_logs(options.repo, gh_run_id, log_path)
    source = GitHubRunSource(
        repo=options.repo,
        run_id=gh_run_id,
        workflow_name=fetched.workflow_name,
        log_path=str(fetched.log_path),
    )

    run = await _run_pipeline(
        run_id=run_id,
        repo_path=repo_path,
        run_dir=run_dir,
        source=source,
        log_path=fetched.log_path,
        verify_command=options.verify_command,
        budget_usd=options.budget,
        mode=options.mode,
        dry_run=options.dry_run,
        repo_slug=options.repo,
        mode_label="github",
    )

    # Optionally create a PR
    if options.create_pr and run.status == "verified" and run.repair:
        policy = policy_mod.load_policy(repo_path)
        draft = policy.repair.create_pr_default == "draft"
        branch = f"patchpilot/fix-{gh_run_id}"
        report_path = run.artifacts.report_path
        body = Path(report_path).read_text() if report_path else "PatchPilot repair"
        try:
            pr_url = await create_pull_request(
                repo=options.repo,
                branch_name=branch,
                title=f"PatchPilot: repair run {gh_run_id}",
                body=body,
                repo_path=repo_path,
                draft=draft,
            )
            print(f"\nDraft PR created: {pr_url}")
            run.status = "pr_created"
            (run_dir / "run.json").write_text(run.model_dump_json(indent=2))
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to create PR: {exc}")

    return run


async def _run_pipeline(  # noqa: PLR0913
    *,
    run_id: str,
    repo_path: Path,
    run_dir: Path,
    source: Any,
    log_path: Path,
    verify_command: str,
    budget_usd: float,
    mode: PatchPilotMode,
    dry_run: bool,
    repo_slug: str | None,
    mode_label: str,
) -> PatchPilotRun:
    """Inner pipeline shared by repair_local and repair_github.

    Each step builds a LedgerStep entry that's persisted at the end via
    audit-agent (or a local fallback if AgentField isn't running).
    """
    steps: list[LedgerStep] = []

    # === Step 1: Read + normalize log ===
    raw_log = _read_log(log_path, source)
    normalized = redactor.normalize_logs(raw_log)
    steps.append(_ledger_step("ingest", "patchpilot", "filesystem", "none"))

    # === Step 2: Redact secrets ===
    redaction = redactor.redact_secrets(normalized)
    redacted_log = redaction.redacted_text
    (run_dir / "redacted.log").write_text(redacted_log)
    steps.append(
        _ledger_step(
            "redact",
            "patchpilot",
            "filesystem",
            "none",
            reason=f"Redacted {redaction.count} secret(s)",
        )
    )

    # === Step 3: Classify (agentic when uncertain) ===
    classification = classifier.classify_failure(redacted_log)
    investigation_log: list[str] = []
    root_cause = f"Pattern-detected {classification.type}"

    if classification.confidence >= 0.85:
        investigation_log.append("Pattern match: high confidence, skipped LLM")
    elif os.getenv("TOKENROUTER_API_KEY"):
        # Iterative reasoning — same logic as triage-agent but inline
        investigation_log.append(
            f"Pattern uncertain ({classification.confidence:.2f}) — invoking LLM reasoning"
        )
        classification, root_cause, investigation_log = await _agentic_classify(
            redacted_log=redacted_log,
            pattern_initial=classification,
            repo_path=repo_path,
            steps=steps,
        )
    else:
        investigation_log.append("No TOKENROUTER_API_KEY — using pattern match as-is")

    steps.append(
        _ledger_step(
            "classify",
            "triage",
            "model" if classification.confidence < 0.85 else "filesystem",
            "free" if classification.confidence < 0.85 else "none",
            reason=f"Classified as {classification.type} (conf={classification.confidence:.2f})",
        )
    )

    # === Step 4: Policy check (failure type) ===
    policy = policy_mod.load_policy(repo_path)
    violation = policy_mod.check_failure_type_allowed(classification, policy)
    steps.append(
        _ledger_step(
            "policy_check",
            "triage",
            "filesystem",
            "none",
            reason=violation or "allowed",
            status="failed" if violation else "success",
        )
    )

    # Triage mode terminates here, dry-run continues but skips repair model call
    if mode == "triage" or violation is not None:
        return _finalize_run(
            run_id=run_id,
            repo_path=repo_path,
            run_dir=run_dir,
            source=source,
            classification=classification,
            repair=None,
            verification=None,
            steps=steps,
            mode=mode,
            mode_label=mode_label,
            repo_slug=repo_slug,
            budget_usd=budget_usd,
            policy_violation=violation,
        )

    # === Step 5: Repair (or dry-run skip) ===
    repair_result: RepairResult | None = None
    if dry_run or mode == "dry-run":
        steps.append(
            _ledger_step("repair", "repair", "model", "none", reason="dry-run skip")
        )
        repair_result = RepairResult(summary="Dry-run — no patch applied")
    else:
        # Local fallback: directly call shared.patch_applier with a stub
        # response. In production this is app.call("repair-agent.repair", ...).
        # For pre-built MVP, we route through agentfield if available, else
        # call OpenAI-compatible endpoint directly via httpx.
        repair_result = await _do_repair_inline(
            repo_path=repo_path,
            redacted_log=redacted_log,
            classification=classification,
            verify_command=verify_command,
            budget_usd=budget_usd,
            policy=policy,
            steps=steps,
        )

    # === Step 6: Verify ===
    verification: VerificationResult | None = None
    if mode == "dry-run" or dry_run:
        steps.append(_ledger_step("verify", "verify", "bash", "none", reason="dry-run skip"))
    elif repair_result and repair_result.files_changed:
        verification = await _do_verify_inline(repo_path, [verify_command])
        steps.append(
            _ledger_step("verify", "verify", "bash", "none", reason=verification.status)
        )

    # === Step 7: Path policy check ===
    if repair_result and repair_result.files_changed:
        path_violations = policy_mod.check_forbidden_paths(repair_result.files_changed, policy)
        if path_violations:
            steps.append(
                _ledger_step(
                    "policy_check_paths",
                    "verify",
                    "filesystem",
                    "none",
                    reason=f"violations: {len(path_violations)}",
                    status="failed",
                )
            )
        else:
            steps.append(
                _ledger_step(
                    "policy_check_paths",
                    "verify",
                    "filesystem",
                    "none",
                    reason="all paths allowed",
                )
            )

    return _finalize_run(
        run_id=run_id,
        repo_path=repo_path,
        run_dir=run_dir,
        source=source,
        classification=classification,
        repair=repair_result,
        verification=verification,
        steps=steps,
        mode=mode,
        mode_label=mode_label,
        repo_slug=repo_slug,
        budget_usd=budget_usd,
        policy_violation=None,
    )


async def _do_repair_inline(  # noqa: PLR0913
    *,
    repo_path: Path,
    redacted_log: str,
    classification: FailureClassification,
    verify_command: str,
    budget_usd: float,
    policy: Any,
    steps: list[LedgerStep],
) -> RepairResult:
    """Invoke repair logic.

    In standalone mode (no AgentField control plane), this calls TokenRouter
    directly via httpx. In production (control plane up), the orchestrator
    should swap this for app.call("repair-agent.repair", ...).

    Returns a RepairResult. On model failure, returns an empty repair with
    diagnostic summary.
    """
    import httpx

    from shared.patch_applier import PatchApplyError, apply_patch_from_response
    from shared.prompts import REPAIR_SYSTEM, build_repair_prompt

    api_key = os.getenv("TOKENROUTER_API_KEY")
    base_url = os.getenv("TOKENROUTER_BASE_URL", "https://api.tokenrouter.com/v1")

    if not api_key:
        steps.append(
            _ledger_step(
                "repair", "repair", "model", "none",
                reason="TOKENROUTER_API_KEY not set; skipping",
                status="skipped",
            )
        )
        return RepairResult(summary="Skipped — no TOKENROUTER_API_KEY")

    # Risk-based tier routing
    if classification.risk == "low":
        model = os.getenv("PATCHPILOT_CHEAP_MODEL", "qwen/qwen3-coder-next")
        tier = "free"
    else:
        model = os.getenv("PATCHPILOT_PRO_MODEL", "qwen/qwen3-coder-next")
        tier = "pro"

    prompt = build_repair_prompt(
        repo_path=str(repo_path),
        failure_log=redacted_log,
        classification=classification,
        verify_command=verify_command,
        budget_usd=budget_usd,
        max_attempts=policy.repair.max_attempts,
        forbidden_paths=policy.repair.forbidden_paths,
    )

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": REPAIR_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        response_text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - start) * 1000)
        steps.append(
            _ledger_step(
                "repair", "repair", "model", tier,
                selected_model=model,
                reason=f"Model call failed: {exc}",
                status="failed",
                duration_ms=duration_ms,
            )
        )
        return RepairResult(summary=f"Model error: {exc}")

    duration_ms = int((time.perf_counter() - start) * 1000)

    # Apply the patch
    try:
        apply_result = await apply_patch_from_response(repo_path, response_text)
    except PatchApplyError as exc:
        steps.append(
            _ledger_step(
                "repair", "repair", "model", tier,
                selected_model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reason=f"Patch apply failed: {exc}",
                status="failed",
                duration_ms=duration_ms,
            )
        )
        return RepairResult(summary=f"Apply failed: {exc}")

    # Cost is reported by TokenRouter in `data["usage"]["total_cost"]` when available.
    # Fallback to rough estimates per token.
    actual_cost = float(usage.get("total_cost", 0.0))
    if actual_cost == 0.0 and tier == "pro":
        # Rough Sonnet pricing
        actual_cost = (input_tokens / 1_000_000 * 3.0) + (output_tokens / 1_000_000 * 15.0)

    steps.append(
        LedgerStep(
            name="repair",
            owner="repair",
            tool="model",
            model_tier=tier,  # type: ignore[arg-type]
            selected_model=model,
            reason=apply_result.summary.summary if apply_result.summary else "patch applied",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=actual_cost,
            actual_cost_usd=actual_cost,
            status="success" if apply_result.files_changed else "failed",
            duration_ms=duration_ms,
        )
    )

    files_str = ", ".join(apply_result.files_changed) or "none"
    return RepairResult(
        files_changed=apply_result.files_changed,
        diff_path="",
        summary=(
            apply_result.summary.summary
            if apply_result.summary and apply_result.summary.summary
            else f"Applied patch via {tier} tier to {files_str}"
        ),
    )


async def _do_verify_inline(repo_path: Path, commands: list[str]) -> VerificationResult:
    """Run verification commands locally."""
    from shared.models import VerificationCommand

    results: list[VerificationCommand] = []
    all_passed = True

    for cmd in commands:
        start = time.perf_counter()
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        duration_ms = int((time.perf_counter() - start) * 1000)
        out_path = repo_path / f".patchpilot-verify-{int(time.time() * 1000)}.log"
        out_path.write_bytes((stdout or b"") + b"\n" + (stderr or b""))

        results.append(
            VerificationCommand(
                command=cmd,
                exit_code=proc.returncode or 0,
                duration_ms=duration_ms,
                output_path=str(out_path),
            )
        )
        if proc.returncode != 0:
            all_passed = False

    status = "verified_pass" if all_passed and results else "failed_after_patch"
    return VerificationResult(status=status, commands=results)  # type: ignore[arg-type]


def _read_log(log_path: Path, source: Any) -> str:
    """Read log content depending on source type."""
    if isinstance(source, LocalCommandSource):
        try:
            stdout = Path(source.stdout_path).read_text(errors="replace")
            stderr = Path(source.stderr_path).read_text(errors="replace")
            return stderr + "\n" + stdout
        except Exception:  # noqa: BLE001
            return ""
    return log_path.read_text(errors="replace") if log_path.exists() else ""


def _ledger_step(  # noqa: PLR0913
    name: str,
    owner: str,
    tool: str,
    model_tier: str,
    *,
    reason: str = "",
    status: str = "success",
    selected_model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int = 0,
) -> LedgerStep:
    """Build a LedgerStep with sensible defaults."""
    return LedgerStep(
        name=name,
        owner=owner,  # type: ignore[arg-type]
        tool=tool,  # type: ignore[arg-type]
        model_tier=model_tier,  # type: ignore[arg-type]
        selected_model=selected_model,
        reason=reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=0.0,
        actual_cost_usd=0.0,
        status=status,  # type: ignore[arg-type]
        duration_ms=duration_ms,
    )


def _make_empty_run(  # noqa: PLR0913
    run_id: str,
    mode_label: str,
    repo_path: Path,
    source: Any,
    budget_usd: float,
) -> PatchPilotRun:
    """Build a minimal Run for the no-failure case."""
    ledger = CostLedger(
        run_id=run_id,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        mode=mode_label,  # type: ignore[arg-type]
        repo=str(repo_path),
        budget_target_usd=budget_usd,
        budget_hard_cap_usd=budget_usd * 2,
        steps=[],
        totals=LedgerTotals(),
    )
    return PatchPilotRun(
        id=run_id,
        mode="full",
        status="failed",
        repo_path=str(repo_path),
        source=source,
        ledger=ledger,
    )


def _finalize_run(  # noqa: PLR0913
    *,
    run_id: str,
    repo_path: Path,
    run_dir: Path,
    source: Any,
    classification: FailureClassification,
    repair: RepairResult | None,
    verification: VerificationResult | None,
    steps: list[LedgerStep],
    mode: PatchPilotMode,
    mode_label: str,
    repo_slug: str | None,
    budget_usd: float,
    policy_violation: str | None,
) -> PatchPilotRun:
    """Compose final PatchPilotRun, persist artifacts, return."""
    actual_cost = sum(s.actual_cost_usd for s in steps)
    pro_equiv = actual_cost / 0.35 if actual_cost > 0 else 0
    savings = max(0.0, pro_equiv - actual_cost)

    ledger = CostLedger(
        run_id=run_id,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        mode=mode_label,  # type: ignore[arg-type]
        repo=str(repo_path),
        budget_target_usd=budget_usd,
        budget_hard_cap_usd=budget_usd * 2,
        steps=steps,
        totals=LedgerTotals(
            actual_cost_usd=actual_cost,
            estimated_pro_equivalent_usd=pro_equiv,
            estimated_savings_usd=savings,
            estimated_savings_percent=(savings / pro_equiv * 100) if pro_equiv > 0 else 0,
        ),
    )

    # Determine final status
    if policy_violation:
        status = "aborted"
    elif mode == "triage":
        status = "diagnosed"
    elif mode == "dry-run":
        status = "diagnosed"
    elif verification and verification.status == "verified_pass":
        status = "verified"
    elif repair and repair.files_changed:
        status = "patched"
    else:
        status = "failed"

    artifacts = RunArtifacts()

    # Persist diff
    if repair and repair.files_changed:
        # `git diff` output (after apply, this captures what's in the working tree)
        proc = asyncio.run(
            asyncio.create_subprocess_exec(
                "git", "diff", cwd=str(repo_path),
                stdout=asyncio.subprocess.PIPE,
            )
        ) if False else None
        # Simpler synchronous approach: skip, we already have the applied diff
        diff_path = run_dir / "patch.diff"
        # The diff text is captured during repair; we don't have it here
        # easily without restructuring — leave the path None for now.
        artifacts.diff_path = str(diff_path)

    # Persist ledger + report
    ledger_path = run_dir / "ledger.json"
    ledger_path.write_text(ledger.model_dump_json(indent=2))
    artifacts.ledger_path = str(ledger_path)

    report = _build_report(
        run_id=run_id,
        mode=mode_label,
        classification=classification,
        repair=repair,
        verification=verification,
        ledger=ledger,
    )
    report_path = run_dir / "report.md"
    report_path.write_text(report)
    artifacts.report_path = str(report_path)

    # Verifiable Credential stub (real signing happens in audit-agent
    # when AgentField is running)
    vc_path = run_dir / "credentials.json"
    vc_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo": repo_slug or str(repo_path),
                "classification_type": classification.type,
                "files_changed": repair.files_changed if repair else [],
                "verification_status": verification.status if verification else "skipped",
                "actual_cost_usd": actual_cost,
                "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "note": "Standalone-mode VC. For cryptographic signing, "
                "run audit-agent under AgentField control plane.",
            },
            indent=2,
        )
    )
    artifacts.vc_path = str(vc_path)

    run = PatchPilotRun(
        id=run_id,
        mode=mode,
        status=status,  # type: ignore[arg-type]
        repo_path=str(repo_path),
        repo_slug=repo_slug,
        source=source,
        classification=classification,
        repair=repair,
        verification=verification,
        ledger=ledger,
        artifacts=artifacts,
    )

    (run_dir / "run.json").write_text(run.model_dump_json(indent=2))
    return run


def _build_report(  # noqa: PLR0913
    *,
    run_id: str,
    mode: str,
    classification: FailureClassification,
    repair: RepairResult | None,
    verification: VerificationResult | None,
    ledger: CostLedger,
) -> str:
    """Render a Markdown PR body."""
    files = ", ".join(repair.files_changed) if (repair and repair.files_changed) else "none"
    verify_line = f"`{verification.status}`" if verification else "skipped"
    repair_summary = repair.summary if repair else "no repair attempted"

    return f"""# PatchPilot Repair Report

## Summary
{repair_summary}

## Failure
- **Type**: {classification.type}
- **Confidence**: {classification.confidence:.2f}
- **Risk**: {classification.risk}
- **Repairability**: {classification.repairability}

## Fix
- **Files changed**: {files}
- **Mode**: {mode}

## Verification
- **Status**: {verify_line}

## Routing and Cost
- **Budget**: ${ledger.budget_target_usd:.2f}
- **Actual spend**: ${ledger.totals.actual_cost_usd:.4f}
- **Estimated pro-tier equivalent**: ${ledger.totals.estimated_pro_equivalent_usd:.4f}
- **Estimated savings**: {ledger.totals.estimated_savings_percent:.0f}%

## Audit
- **Run ID**: `{run_id}`
- **Workflow DAG**: queryable via AgentField control plane (when deployed)
- **Verifiable Credential**: see `credentials.json` in run artifacts

## Review Checklist
- [ ] Confirm the failure matches the original CI issue
- [ ] Review changed files
- [ ] Confirm CI passes on GitHub
- [ ] Confirm no sensitive files were modified

## Notes
This report was generated by PatchPilot v2 using AgentField. Human review is required before merge.
"""


async def _agentic_classify(
    *,
    redacted_log: str,
    pattern_initial: Any,
    repo_path: Path,
    steps: list[LedgerStep],
) -> tuple[Any, str, list[str]]:
    """Standalone iterative classification (mirrors triage-agent logic).

    Uses TokenRouter directly via httpx for LLM calls. Iterates up to 3
    times, reading files the model suggests, until confident or budget out.

    Returns (classification, root_cause, investigation_log).
    """
    import httpx
    from shared.models import FailureClassification, TriageHypothesis, InvestigationAction

    api_key = os.getenv("TOKENROUTER_API_KEY", "")
    base_url = os.getenv("TOKENROUTER_BASE_URL", "https://api.tokenrouter.com/v1")
    model = os.getenv("PATCHPILOT_FREE_MODEL", "qwen/qwen3-coder-next")

    investigation_log: list[str] = []
    files_read: dict[str, str] = {}
    best = pattern_initial
    root_cause = f"Pattern-detected {best.type}"

    for iteration in range(3):  # max 3 reasoning rounds
        # Build context
        files_ctx = ""
        if files_read:
            files_ctx = "\n\nFiles read:\n" + "\n".join(
                f"--- {k} ---\n{v}" for k, v in files_read.items()
            )

        prompt = (
            f"Classify this CI failure. Report confidence honestly.\n\n"
            f"Log:\n```\n{redacted_log[:4000]}\n```\n\n"
            f"Pattern guess: {best.type} (conf {best.confidence:.2f})"
            f"{files_ctx}\n\n"
            f"Respond with JSON: {{\"failure_type\": \"...\", \"confidence\": 0.X, "
            f"\"root_cause\": \"...\", \"needs_investigation\": [{{\"action\": \"read_file\", "
            f"\"target\": \"path\", \"reason\": \"why\"}}]}}"
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            investigation_log.append(f"LLM call failed: {exc}")
            break

        # Try parse response
        import json as _json
        try:
            # Extract JSON from response (might be wrapped in markdown)
            import re
            json_match = re.search(r"\{[\s\S]*\}", content)
            if not json_match:
                investigation_log.append(f"Iter {iteration+1}: unparseable response")
                break
            data = _json.loads(json_match.group())
        except Exception:
            investigation_log.append(f"Iter {iteration+1}: JSON parse failed")
            break

        ft = data.get("failure_type", best.type)
        conf = float(data.get("confidence", best.confidence))
        root_cause = data.get("root_cause", root_cause)
        investigation_log.append(f"Iter {iteration+1}: {ft} (conf {conf:.2f}) — {root_cause[:80]}")

        # Update best
        from typing import get_args
        from shared.models import FailureType
        if ft in get_args(FailureType):
            best = FailureClassification(
                type=ft,
                confidence=conf,
                evidence=best.evidence,
                likely_files=best.likely_files,
                repairability=best.repairability,
                risk=best.risk,
            )

        # Confident enough?
        if conf >= 0.75:
            break

        # Investigate what model asked for
        needs = data.get("needs_investigation", [])
        if not needs:
            break
        for action_data in needs[:3]:
            target = action_data.get("target", "")
            full_path = repo_path / target
            if full_path.exists() and full_path.is_file() and full_path.stat().st_size < 50_000:
                content_read = full_path.read_text(errors="replace")[:2000]
                files_read[target] = content_read
                investigation_log.append(f"  → read {target}: {len(content_read)} chars")

    return best, root_cause, investigation_log
