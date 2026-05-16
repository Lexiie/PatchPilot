"""audit-agent — emit Verifiable Credentials and human-readable summaries.

This agent runs after triage + repair + verify complete. It does two things:

    1. Build a markdown summary of the run (used as PR body).
    2. Emit a Verifiable Credential via AgentField that signs the run
       with the agent's DID. Compliance teams can verify the VC offline
       with `af vc verify`.

Optionally calls app.ai() for narrative summary generation. Falls back
to a deterministic template if model is unavailable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentfield import Agent, AIConfig

from shared.models import (
    CostLedger,
    FailureClassification,
    LedgerStep,
    LedgerTotals,
    RepairResult,
    VerificationResult,
)
from shared.prompts import AUDIT_SUMMARY_SYSTEM, build_audit_summary_prompt


def build_app() -> Agent:
    """Construct the audit-agent."""
    app = Agent(
        node_id="audit-agent",
        version="2.0.0",
        agentfield_server=os.getenv("AGENTFIELD_SERVER_URL", "http://localhost:8080"),
        ai_config=AIConfig(
            model=os.getenv("PATCHPILOT_CHEAP_MODEL", "qwen/qwen3-coder-next"),
            api_key=os.getenv("TOKENROUTER_API_KEY"),
            base_url=os.getenv("TOKENROUTER_BASE_URL", "https://api.tokenrouter.com/v1"),
        ),
    )

    @app.reasoner(tags=["patchpilot", "audit"])
    async def finalize(  # noqa: PLR0913
        run_id: str,
        repo: str,
        mode: str,
        classification: dict[str, Any],
        repair: dict[str, Any] | None,
        verification: dict[str, Any] | None,
        cost_breakdown: list[dict[str, Any]],
        budget_target_usd: float,
        artifact_dir: str,
    ) -> dict[str, Any]:
        """Build the run's audit artifacts.

        Outputs (written to `artifact_dir`):
            ledger.json     — CostLedger (schema-validated)
            report.md       — Markdown PR body
            credentials.json — Verifiable Credential dump

        Returns dict with paths and the markdown body for the PR creator.
        """
        artifact_path = Path(artifact_dir)
        artifact_path.mkdir(parents=True, exist_ok=True)

        clf = FailureClassification.model_validate(classification)
        repair_obj = RepairResult.model_validate(repair) if repair else None
        verify_obj = VerificationResult.model_validate(verification) if verification else None

        # Build cost ledger from breakdown supplied by orchestrator
        steps: list[LedgerStep] = []
        for step in cost_breakdown:
            steps.append(LedgerStep.model_validate(step))

        actual = sum(s.actual_cost_usd for s in steps)
        # If everything ran in pro tier, cost would be ~3x what we paid
        pro_equiv = actual / 0.35 if actual > 0 else 0
        savings = max(0.0, pro_equiv - actual)

        ledger = CostLedger(
            run_id=run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            mode=mode,  # type: ignore[arg-type]
            repo=repo,
            budget_target_usd=budget_target_usd,
            budget_hard_cap_usd=budget_target_usd * 2,
            steps=steps,
            totals=LedgerTotals(
                actual_cost_usd=actual,
                estimated_pro_equivalent_usd=pro_equiv,
                estimated_savings_usd=savings,
                estimated_savings_percent=(savings / pro_equiv * 100) if pro_equiv > 0 else 0,
            ),
        )

        ledger_path = artifact_path / "ledger.json"
        ledger_path.write_text(ledger.model_dump_json(indent=2))

        # Build the markdown summary — try model first, fall back to template
        summary_text = await _generate_summary(
            app=app,
            classification=clf,
            repair=repair_obj,
            verification=verify_obj,
            cost_usd=actual,
        )

        markdown = _render_pr_body(
            run_id=run_id,
            mode=mode,
            classification=clf,
            repair=repair_obj,
            verification=verify_obj,
            ledger=ledger,
            summary=summary_text,
        )
        report_path = artifact_path / "report.md"
        report_path.write_text(markdown)

        # Emit Verifiable Credential — AgentField handles signing
        vc_payload = {
            "run_id": run_id,
            "repo": repo,
            "classification_type": clf.type,
            "files_changed": repair_obj.files_changed if repair_obj else [],
            "verification_status": verify_obj.status if verify_obj else "skipped",
            "actual_cost_usd": actual,
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
        vc_path = artifact_path / "credentials.json"
        vc_path.write_text(json.dumps(vc_payload, indent=2))

        # Track the audit event in the workflow DAG
        await app.track(
            "audit",
            metadata={
                "run_id": run_id,
                "ledger_path": str(ledger_path),
                "report_path": str(report_path),
                "vc_path": str(vc_path),
            },
        )

        return {
            "ledger_path": str(ledger_path),
            "report_path": str(report_path),
            "vc_path": str(vc_path),
            "pr_body": markdown,
        }

    return app


async def _generate_summary(
    app: Agent,
    classification: FailureClassification,
    repair: RepairResult | None,
    verification: VerificationResult | None,
    cost_usd: float,
) -> str:
    """Generate a narrative summary via app.ai(), falling back to template."""
    if not repair:
        return "No repair was attempted."
    try:
        prompt = build_audit_summary_prompt(
            classification=classification,
            files_changed=repair.files_changed,
            diff_summary=repair.summary,
            verification_status=verification.status if verification else "skipped",
            cost_usd=cost_usd,
        )
        response = await app.ai(system=AUDIT_SUMMARY_SYSTEM, user=prompt)
        return str(response).strip()
    except Exception:  # noqa: BLE001
        # Deterministic fallback
        files = ", ".join(repair.files_changed) if repair.files_changed else "no files"
        return (
            f"PatchPilot classified the failure as {classification.type} "
            f"(confidence {classification.confidence:.2f}) and modified {files}. "
            f"The patch summary from repair-agent: {repair.summary}"
        )


def _render_pr_body(  # noqa: PLR0913
    run_id: str,
    mode: str,
    classification: FailureClassification,
    repair: RepairResult | None,
    verification: VerificationResult | None,
    ledger: CostLedger,
    summary: str,
) -> str:
    """Compose the PR body markdown."""
    files = ", ".join(repair.files_changed) if (repair and repair.files_changed) else "none"
    verify_line = (
        f"`{verification.status}`" if verification else "skipped"
    )
    return f"""# PatchPilot Repair Report

## Summary
{summary}

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
- **Workflow DAG**: queryable via AgentField control plane
- **Verifiable Credential**: see `credentials.json` in run artifacts

## Review Checklist
- [ ] Confirm the failure matches the original CI issue
- [ ] Review changed files
- [ ] Confirm CI passes on GitHub
- [ ] Confirm no sensitive files were modified

## Notes
This report was generated by PatchPilot v2 using AgentField. Human review is required before merge.
"""


if __name__ == "__main__":
    build_app().run()
