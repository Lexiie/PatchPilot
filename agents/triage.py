"""triage-agent — agentic failure classification with guardrails.

This agent genuinely reasons about CI failures. It doesn't just regex-match:
it investigates, forms hypotheses, and refines them iteratively.

Guardrails:
    - Pattern match short-circuits high-confidence cases ($0)
    - Hard budget cap per invocation ($0.02 default, 5 iterations max)
    - Soft warn at 80% budget
    - Agent MUST self-report confidence + what would help (search-before-guess)
    - Graceful degrade to triage_only when uncertain after exhausting budget

Reasoning loop:
    1. Pattern match → if confidence >= 0.85, done (free)
    2. Ask LLM: "What's your hypothesis? What files would help?"
    3. Read those files (capped at 3 per round, 2KB each)
    4. Re-ask with new evidence
    5. Repeat until confident OR budget exhausted
    6. Enforce policy on final classification
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

from agentfield import Agent, AIConfig
from pydantic import BaseModel

from shared.classifier import classify_failure
from shared.models import (
    AgentBudget,
    ClassifyResult,
    FailureClassification,
    InvestigationAction,
    RedactionResult,
    TriageHypothesis,
)
from shared.policy import check_failure_type_allowed, load_policy
from shared.redactor import normalize_logs, redact_secrets


# ─── Repairability / risk mapping ─────────────────────────────────────────

_REPAIRABILITY_MAP = {
    "lint": "safe_auto_patch",
    "format": "safe_auto_patch",
    "typecheck": "safe_auto_patch",
    "unit_test": "patch_with_review",
    "integration_test": "patch_with_review",
    "dependency_config": "patch_with_review",
    "package_lock": "safe_auto_patch",
    "snapshot": "safe_auto_patch",
    "build_compile": "patch_with_review",
    "environment_missing_secret": "do_not_attempt",
    "network_or_infra": "do_not_attempt",
    "flaky_test": "triage_only",
    "unknown": "triage_only",
}

_RISK_MAP = {
    "lint": "low",
    "format": "low",
    "typecheck": "low",
    "unit_test": "medium",
    "integration_test": "medium",
    "dependency_config": "low",
    "package_lock": "low",
    "snapshot": "low",
    "build_compile": "medium",
    "environment_missing_secret": "high",
    "network_or_infra": "high",
    "flaky_test": "medium",
    "unknown": "medium",
}


def build_app() -> Agent:
    """Construct the triage-agent."""
    app = Agent(
        node_id="triage-agent",
        version="2.0.0",
        agentfield_server=os.getenv("AGENTFIELD_SERVER_URL", "http://localhost:8080"),
        ai_config=AIConfig(
            model=os.getenv("PATCHPILOT_FREE_MODEL", "qwen/qwen3-coder-next"),
            api_key=os.getenv("TOKENROUTER_API_KEY"),
            base_url=os.getenv("TOKENROUTER_BASE_URL", "https://api.tokenrouter.com/v1"),
        ),
    )

    @app.reasoner(tags=["patchpilot", "triage", "agentic"])
    async def classify(failure_log: str, repo_path: str) -> dict[str, Any]:
        """Classify a CI failure with iterative reasoning and guardrails.

        The agent investigates until confident or budget-exhausted. It never
        guesses — if uncertain, it either investigates more or escalates.
        """
        budget = AgentBudget(
            max_cost_usd=float(os.getenv("PATCHPILOT_TRIAGE_BUDGET", "0.02")),
            max_iterations=int(os.getenv("PATCHPILOT_TRIAGE_MAX_ITER", "5")),
        )

        # Phase 1: deterministic prep (free)
        normalized = normalize_logs(failure_log)
        redaction = redact_secrets(normalized)
        redacted = redaction.redacted_text

        # Phase 2: pattern match — short-circuit if very confident
        pattern_result = classify_failure(redacted)
        if pattern_result.confidence >= 0.85:
            await app.note(
                f"Pattern match confident: {pattern_result.type} ({pattern_result.confidence:.2f})",
                tags=["triage", "fast-path"],
            )
            return _build_result(
                classification=pattern_result,
                redaction=redaction,
                repo_path=repo_path,
                investigation_log=["Pattern match: high confidence, skipped LLM"],
                root_cause=f"Pattern-detected {pattern_result.type}",
                budget=budget,
            )

        # Phase 3: agentic reasoning loop
        await app.note(
            f"Pattern match uncertain ({pattern_result.type}, conf={pattern_result.confidence:.2f}) "
            f"— beginning iterative investigation",
            tags=["triage", "investigate"],
        )

        investigation_log: list[str] = []
        files_read: dict[str, str] = {}
        hypothesis: TriageHypothesis | None = None

        while True:
            ok, reason = budget.can_continue()
            if not ok:
                await app.note(f"Triage budget exhausted: {reason}", tags=["triage", "budget"])
                investigation_log.append(f"Budget exhausted: {reason}")
                break

            if budget.at_soft_limit():
                await app.note("Triage at 80% budget", tags=["triage", "budget", "warn"])

            # Reason with current evidence
            hypothesis = await _hypothesize(
                app=app,
                redacted_log=redacted,
                pattern_initial=pattern_result,
                files_read=files_read,
                previous=hypothesis,
            )
            budget.iterations += 1
            # Estimate cost: ~$0.003 per reasoning call (free-tier model)
            budget.spent_usd += 0.003

            investigation_log.append(
                f"Iter {budget.iterations}: {hypothesis.failure_type} "
                f"(conf {hypothesis.confidence:.2f}) — {hypothesis.confidence_reasoning}"
            )

            # Agent says it's confident enough
            if hypothesis.can_proceed and hypothesis.confidence >= 0.75:
                await app.note(
                    f"Agent confident: {hypothesis.failure_type} ({hypothesis.confidence:.2f})",
                    tags=["triage", "converged"],
                )
                break

            # Search-before-guess: agent must say what would help
            if not hypothesis.needs_investigation:
                await app.note(
                    "Agent uncertain with no investigation path — escalating to triage_only",
                    tags=["triage", "escalate"],
                )
                investigation_log.append("No investigation path — escalating")
                break

            # Execute investigations (capped at 3 per round)
            for action in hypothesis.needs_investigation[:3]:
                content = await _execute_investigation(repo_path, action)
                if content is not None:
                    key = f"{action.action}:{action.target}"
                    files_read[key] = content[:2000]
                    investigation_log.append(
                        f"  → {action.action} {action.target}: {len(content)} chars"
                    )

        # Phase 4: build final classification
        if hypothesis is None or hypothesis.confidence < 0.4:
            final = FailureClassification(
                type="unknown",
                confidence=0.1,
                evidence=[],
                likely_files=[],
                repairability="triage_only",
                risk="medium",
            )
            root_cause = "Could not determine root cause within budget"
        else:
            final = FailureClassification(
                type=hypothesis.failure_type,
                confidence=hypothesis.confidence,
                evidence=hypothesis.evidence,
                likely_files=hypothesis.likely_files,
                repairability=_REPAIRABILITY_MAP.get(hypothesis.failure_type, "triage_only"),
                risk=_RISK_MAP.get(hypothesis.failure_type, "medium"),
            )
            root_cause = hypothesis.root_cause

        return _build_result(
            classification=final,
            redaction=redaction,
            repo_path=repo_path,
            investigation_log=investigation_log,
            root_cause=root_cause,
            budget=budget,
        )

    return app


async def _hypothesize(
    *,
    app: Agent,
    redacted_log: str,
    pattern_initial: FailureClassification,
    files_read: dict[str, str],
    previous: TriageHypothesis | None,
) -> TriageHypothesis:
    """Ask the LLM for a hypothesis with explicit uncertainty reporting."""
    files_context = ""
    if files_read:
        files_context = "\n\n## Files investigated so far\n" + "\n\n".join(
            f"### {path}\n```\n{content}\n```" for path, content in files_read.items()
        )

    previous_context = ""
    if previous:
        previous_context = (
            f"\n\n## Previous hypothesis\n"
            f"- Type: {previous.failure_type} (confidence {previous.confidence:.2f})\n"
            f"- Reasoning: {previous.confidence_reasoning}\n"
            f"- Root cause: {previous.root_cause}"
        )

    result = await app.ai(
        system=(
            "You are a CI failure investigator. Analyze the failure log and produce a "
            "structured hypothesis. You MUST:\n"
            "1. Report your confidence honestly (0.0-1.0)\n"
            "2. Explain WHY you have that confidence level\n"
            "3. If confidence < 0.75, list concrete investigation actions that would help\n"
            "4. Never guess — if you don't know, say so and ask for more evidence\n"
            "5. Set can_proceed=true ONLY if you're genuinely confident (>= 0.75)"
        ),
        user=(
            f"## Failure log (redacted)\n```\n{redacted_log[:4000]}\n```\n\n"
            f"## Pattern matcher initial guess\n"
            f"- Type: {pattern_initial.type} (confidence {pattern_initial.confidence:.2f})\n"
            f"- Evidence: {', '.join(pattern_initial.evidence) or 'none'}\n"
            f"- Likely files: {', '.join(pattern_initial.likely_files) or 'unknown'}"
            f"{files_context}{previous_context}\n\n"
            f"Produce your hypothesis. Be honest about uncertainty."
        ),
        schema=TriageHypothesis,
        temperature=0.2,
    )

    # app.ai with schema returns the parsed object directly
    if isinstance(result, TriageHypothesis):
        return result
    # Fallback: if result is a string or dict, try to parse
    if isinstance(result, dict):
        return TriageHypothesis.model_validate(result)
    # Last resort: use pattern result
    return TriageHypothesis(
        failure_type=pattern_initial.type,
        confidence=pattern_initial.confidence,
        root_cause="LLM response unparseable, using pattern match",
        evidence=pattern_initial.evidence,
        likely_files=pattern_initial.likely_files,
        confidence_reasoning="Fallback to pattern match",
        needs_investigation=[],
        can_proceed=pattern_initial.confidence >= 0.75,
    )


async def _execute_investigation(repo_path: str, action: InvestigationAction) -> str | None:
    """Execute a single investigation action safely.

    Returns file content or command output. Returns None if action fails
    or target doesn't exist. Capped at 2KB output.
    """
    repo = Path(repo_path)

    if action.action == "read_file":
        target = repo / action.target
        if not target.exists() or not target.is_file():
            return None
        if target.stat().st_size > 50_000:
            return None
        try:
            return target.read_text(errors="replace")[:2000]
        except Exception:
            return None

    elif action.action == "grep":
        try:
            proc = await asyncio.create_subprocess_exec(
                "grep", "-rn", "--include=*.py", "--include=*.ts", "--include=*.js",
                "--include=*.go", "--include=*.rs", "--include=*.java", "--include=*.rb",
                action.target, str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return stdout.decode(errors="replace")[:2000] or None
        except Exception:
            return None

    elif action.action == "git_log":
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "log", "--oneline", "-10",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return stdout.decode(errors="replace")[:2000] or None
        except Exception:
            return None

    elif action.action == "list_dir":
        target = repo / action.target
        if not target.exists() or not target.is_dir():
            return None
        try:
            entries = sorted(target.iterdir())[:50]
            return "\n".join(str(e.relative_to(repo)) for e in entries)
        except Exception:
            return None

    return None


def _build_result(
    *,
    classification: FailureClassification,
    redaction: RedactionResult,
    repo_path: str,
    investigation_log: list[str],
    root_cause: str,
    budget: AgentBudget,
) -> dict[str, Any]:
    """Compose the final ClassifyResult dict."""
    policy = load_policy(Path(repo_path))
    violation = check_failure_type_allowed(classification, policy)

    result = ClassifyResult(
        classification=classification,
        redaction=redaction,
        policy=policy,
        policy_violation=violation,
        proceed_to_repair=(
            violation is None
            and classification.repairability not in ("do_not_attempt", "triage_only")
        ),
        investigation_log=investigation_log,
        root_cause=root_cause,
        budget_used_usd=budget.spent_usd,
        iterations=budget.iterations,
    )
    return result.model_dump()


if __name__ == "__main__":
    build_app().run()
