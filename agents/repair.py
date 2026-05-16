"""repair-agent — agentic multi-turn repair via Harness with guardrails.

This agent uses AgentField's app.harness() to dispatch a multi-turn coding
agent (Claude Code, Codex, etc.) that iteratively investigates, patches,
and verifies the fix.

Guardrails:
    - Confidence gate: won't attempt repair if classification confidence < 0.6
    - Plan-first: permission_mode="plan" forces agent to plan before editing
    - Hard budget cap per invocation (max_budget_usd)
    - Turn cap (max_turns)
    - Post-execution forbidden-path check + rollback
    - Agent self-veto: if Harness returns success=false, we accept gracefully

The repair agent is the ONLY agent that may spend significant money.
Low-risk failures route to free-tier models; medium-risk to pro-tier.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agentfield import Agent, AIConfig

from shared.models import (
    FailureClassification,
    HarnessRepairResult,
    RepairAttempt,
    RepairResult,
)
from shared.policy import check_forbidden_paths, load_policy


def build_app() -> Agent:
    """Construct the repair-agent."""
    app = Agent(
        node_id="repair-agent",
        version="2.0.0",
        agentfield_server=os.getenv("AGENTFIELD_SERVER_URL", "http://localhost:8080"),
        ai_config=AIConfig(
            model=os.getenv("PATCHPILOT_PRO_MODEL", "qwen/qwen3-coder-next"),
            api_key=os.getenv("TOKENROUTER_API_KEY"),
            base_url=os.getenv("TOKENROUTER_BASE_URL", "https://api.tokenrouter.com/v1"),
        ),
    )

    @app.reasoner(tags=["patchpilot", "repair", "agentic"])
    async def repair(
        repo_path: str,
        redacted_log: str,
        classification: dict[str, Any],
        verify_command: str,
        budget_usd: float = 0.20,
        max_attempts: int = 3,
        forbidden_paths: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Multi-turn repair with plan-first guardrail and budget cap.

        The agent:
        1. Checks confidence gate (won't attempt if < 0.6)
        2. Dispatches to Harness with permission_mode="plan"
        3. Harness plans → executes → verifies iteratively
        4. Post-check: forbidden paths violated? → rollback
        5. Returns structured RepairResult
        """
        clf = FailureClassification.model_validate(classification)
        forbidden = forbidden_paths or []

        # ─── Guardrail 1: confidence gate ─────────────────────────────
        if clf.confidence < 0.6:
            await app.note(
                f"Classification confidence too low ({clf.confidence:.2f}) — declining repair",
                tags=["repair", "gate", "confidence"],
            )
            return RepairResult(
                summary=f"Repair declined: classification confidence {clf.confidence:.2f} < 0.6 threshold",
            ).model_dump()

        # ─── Guardrail 2: dry-run / triage mode ──────────────────────
        if dry_run:
            await app.note("Dry-run mode — skipping repair", tags=["repair", "dry-run"])
            return RepairResult(summary="Dry-run — no repair attempted").model_dump()

        # ─── Route by risk ────────────────────────────────────────────
        if clf.risk == "low":
            provider = "claude-code"
            model = os.getenv("PATCHPILOT_FREE_MODEL", "qwen/qwen3-coder-next")
            tier_budget = min(budget_usd, 0.05)  # cap low-risk spend
        else:
            provider = "claude-code"
            model = os.getenv("PATCHPILOT_PRO_MODEL", "qwen/qwen3-coder-next")
            tier_budget = budget_usd

        await app.note(
            f"Routing repair: risk={clf.risk}, model={model}, budget=${tier_budget:.2f}",
            tags=["repair", "route", clf.risk],
        )

        # ─── Build task for Harness ───────────────────────────────────
        forbidden_str = ", ".join(forbidden) if forbidden else "none"
        likely_str = ", ".join(clf.likely_files) if clf.likely_files else "explore the repo"

        task = (
            f"Fix this failing CI command: `{verify_command}`\n\n"
            f"## Failure info\n"
            f"- Type: {clf.type}\n"
            f"- Risk: {clf.risk}\n"
            f"- Likely files: {likely_str}\n\n"
            f"## Failure log (redacted)\n```\n{redacted_log[:6000]}\n```\n\n"
            f"## Instructions\n"
            f"1. Investigate the repo to understand the failure\n"
            f"2. Read relevant source and test files\n"
            f"3. Generate a minimal patch\n"
            f"4. Apply it\n"
            f"5. Run `{verify_command}` to verify\n"
            f"6. If verify fails, read the error, refine, retry\n"
            f"7. Stop when verify passes OR you're stuck\n\n"
            f"## Constraints\n"
            f"- Forbidden paths (NEVER modify): {forbidden_str}\n"
            f"- Make minimal changes only\n"
            f"- Do not add dependencies unless strictly necessary\n"
            f"- If you cannot fix it cleanly, report success=false\n"
        )

        # ─── Harness call with guardrails ─────────────────────────────
        max_turns = max_attempts * 3  # ~3 turns per logical attempt

        try:
            harness_result = await app.harness(
                task,
                provider=provider,
                model=model,
                schema=HarnessRepairResult,
                max_turns=max_turns,
                max_budget_usd=tier_budget,
                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
                permission_mode="plan",  # Forces plan-before-execute
                cwd=repo_path,
                system_prompt=(
                    "You are PatchPilot's repair agent. Make minimal, targeted fixes. "
                    "Always verify by running the test command after editing. "
                    "If you cannot fix the issue cleanly, return success=false. "
                    "Never modify forbidden paths. Never guess — investigate first."
                ),
            )
        except Exception as exc:
            await app.note(f"Harness invocation failed: {exc}", tags=["repair", "error"])
            return RepairResult(
                summary=f"Harness error: {exc}",
                attempts=[
                    RepairAttempt(
                        attempt_number=1,
                        model=model,
                        duration_ms=0,
                        success=False,
                        summary=str(exc),
                    )
                ],
            ).model_dump()

        # ─── Parse Harness result ─────────────────────────────────────
        if harness_result.is_error:
            await app.note(
                f"Harness error: {harness_result.error_message} "
                f"(failure_type={harness_result.failure_type})",
                tags=["repair", "harness-error"],
            )
            return RepairResult(
                summary=f"Harness failed: {harness_result.error_message}",
                attempts=[
                    RepairAttempt(
                        attempt_number=1,
                        model=model,
                        duration_ms=harness_result.duration_ms,
                        success=False,
                        summary=harness_result.error_message or "unknown error",
                    )
                ],
            ).model_dump()

        # Extract parsed schema result
        parsed: HarnessRepairResult | None = None
        if harness_result.parsed and isinstance(harness_result.parsed, HarnessRepairResult):
            parsed = harness_result.parsed
        elif harness_result.parsed and isinstance(harness_result.parsed, dict):
            parsed = HarnessRepairResult.model_validate(harness_result.parsed)

        files_changed = parsed.files_changed if parsed else []
        success = parsed.success if parsed else False
        summary = parsed.summary if parsed else (harness_result.result or "no output")

        # ─── Guardrail 3: post-execution forbidden path check ─────────
        if files_changed:
            policy = load_policy(Path(repo_path))
            violations = check_forbidden_paths(files_changed, policy)
            if violations:
                await app.note(
                    f"Harness violated forbidden paths: {violations} — rolling back",
                    tags=["repair", "violation", "rollback"],
                )
                await _git_checkout_all(repo_path)
                return RepairResult(
                    files_changed=[],
                    summary=f"Repair rejected: modified forbidden paths {violations}",
                    attempts=[
                        RepairAttempt(
                            attempt_number=1,
                            model=model,
                            duration_ms=harness_result.duration_ms,
                            success=False,
                            summary=f"Forbidden path violation: {violations}",
                        )
                    ],
                ).model_dump()

        # ─── Success path ─────────────────────────────────────────────
        await app.note(
            f"Repair complete: success={success}, files={len(files_changed)}, "
            f"turns={harness_result.num_turns}, cost=${harness_result.cost_usd or 0:.4f}",
            tags=["repair", "done"],
        )

        return RepairResult(
            files_changed=files_changed,
            summary=f"{summary} (turns={harness_result.num_turns}, cost=${harness_result.cost_usd or 0:.4f})",
            attempts=[
                RepairAttempt(
                    attempt_number=1,
                    model=model,
                    duration_ms=harness_result.duration_ms,
                    success=success,
                    summary=summary,
                )
            ],
        ).model_dump()

    return app


async def _git_checkout_all(repo_path: str) -> None:
    """Roll back all working tree changes (non-destructive to untracked)."""
    proc = await asyncio.create_subprocess_exec(
        "git", "checkout", "--", ".",
        cwd=repo_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


if __name__ == "__main__":
    build_app().run()
