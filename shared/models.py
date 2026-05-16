"""Pydantic schemas shared across PatchPilot agents.

These models define the contract for inter-agent communication via AgentField's
app.call() and the data shape for run artifacts (ledger.json, report.md).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

# ─── Failure classification ────────────────────────────────────────────────

FailureType = Literal[
    "lint",
    "format",
    "typecheck",
    "unit_test",
    "integration_test",
    "dependency_config",
    "package_lock",
    "snapshot",
    "build_compile",
    "environment_missing_secret",
    "network_or_infra",
    "flaky_test",
    "unknown",
]

Repairability = Literal[
    "safe_auto_patch",
    "patch_with_review",
    "triage_only",
    "do_not_attempt",
]

Risk = Literal["low", "medium", "high"]

ModelTier = Literal["free", "pro", "none"]


class FailureClassification(BaseModel):
    """Output of the triage-agent classifier."""

    type: FailureType
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    likely_files: list[str] = Field(default_factory=list)
    repairability: Repairability
    risk: Risk


# ─── Failure source ────────────────────────────────────────────────────────


class LocalCommandSource(BaseModel):
    type: Literal["local_command"] = "local_command"
    command: str
    exit_code: int
    stdout_path: str
    stderr_path: str


class LogFileSource(BaseModel):
    type: Literal["log_file"] = "log_file"
    log_path: str


class GitHubRunSource(BaseModel):
    type: Literal["github_run"] = "github_run"
    repo: str
    run_id: str
    workflow_name: str | None = None
    job_name: str | None = None
    log_path: str


FailureSource = LocalCommandSource | LogFileSource | GitHubRunSource


# ─── Redaction result ──────────────────────────────────────────────────────


class RedactionResult(BaseModel):
    redacted_text: str
    count: int
    matched_patterns: list[str]


# ─── Repair ────────────────────────────────────────────────────────────────


class RepairAttempt(BaseModel):
    attempt_number: int
    model: str | None = None
    duration_ms: int
    success: bool
    summary: str


class RepairResult(BaseModel):
    branch_name: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    diff_path: str = ""
    summary: str = ""
    attempts: list[RepairAttempt] = Field(default_factory=list)


# ─── Verification ──────────────────────────────────────────────────────────


VerificationStatus = Literal[
    "verified_pass",
    "partial_pass",
    "failed_after_patch",
    "not_reproducible",
    "triage_only",
]


class VerificationCommand(BaseModel):
    command: str
    exit_code: int
    duration_ms: int
    output_path: str


class VerificationResult(BaseModel):
    status: VerificationStatus
    commands: list[VerificationCommand] = Field(default_factory=list)


# ─── Cost ledger ───────────────────────────────────────────────────────────


class LedgerStep(BaseModel):
    """A single entry in the cost ledger.

    Each agent invocation produces one or more LedgerStep entries via
    AgentField's workflow DAG. Cost data comes from TokenRouter responses
    (when an LLM is called) or is zero for deterministic steps.
    """

    name: str
    owner: Literal["triage", "repair", "verify", "audit", "patchpilot", "shell"]
    tool: Literal["model", "bash", "git", "gh", "filesystem"]
    model_tier: ModelTier
    selected_model: str | None = None
    reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0
    status: Literal["success", "failed", "skipped"] = "success"
    duration_ms: int = 0


class LedgerTotals(BaseModel):
    actual_cost_usd: float = 0.0
    estimated_pro_equivalent_usd: float = 0.0
    estimated_savings_usd: float = 0.0
    estimated_savings_percent: float = 0.0


class CostLedger(BaseModel):
    run_id: str
    started_at: str
    completed_at: str | None = None
    mode: Literal["local", "log", "github", "managed"]
    repo: str
    budget_target_usd: float
    budget_hard_cap_usd: float
    steps: list[LedgerStep] = Field(default_factory=list)
    totals: LedgerTotals = Field(default_factory=LedgerTotals)


# ─── Policy ────────────────────────────────────────────────────────────────


class PolicyRepairConfig(BaseModel):
    max_attempts: int = 3
    create_pr_default: Literal["draft", "ready"] = "draft"
    allowed_failure_types: list[FailureType] = Field(
        default_factory=lambda: ["lint", "format", "typecheck", "unit_test", "dependency_config"]
    )
    forbidden_failure_types: list[FailureType] = Field(
        default_factory=lambda: ["environment_missing_secret", "network_or_infra"]
    )
    forbidden_paths: list[str] = Field(default_factory=lambda: [".env*", "secrets/**"])
    require_human_review_for: list[str] = Field(
        default_factory=lambda: ["auth/**", "billing/**", "payments/**", "migrations/**"]
    )


class PolicyVerificationConfig(BaseModel):
    required_commands: list[str] = Field(default_factory=list)


class PolicyAuditConfig(BaseModel):
    vc_signing_did: str = "did:key:auto"
    retention_days: int = 90


class PolicyConfig(BaseModel):
    version: int = 2
    repair: PolicyRepairConfig = Field(default_factory=PolicyRepairConfig)
    verification: PolicyVerificationConfig = Field(default_factory=PolicyVerificationConfig)
    audit: PolicyAuditConfig = Field(default_factory=PolicyAuditConfig)


# ─── Run ───────────────────────────────────────────────────────────────────


RunStatus = Literal[
    "started",
    "diagnosed",
    "patched",
    "verified",
    "pr_created",
    "failed",
    "aborted",
]

PatchPilotMode = Literal["full", "triage", "dry-run"]


class RunArtifacts(BaseModel):
    report_path: str | None = None
    ledger_path: str | None = None
    diff_path: str | None = None
    vc_path: str | None = None
    log_paths: list[str] = Field(default_factory=list)


class PatchPilotRun(BaseModel):
    """Top-level container for a single PatchPilot execution.

    Persisted at .patchpilot/runs/<run_id>/run.json.
    """

    id: str
    mode: PatchPilotMode = "full"
    status: RunStatus = "started"
    repo_path: str | None = None
    repo_slug: str | None = None
    source: FailureSource
    classification: FailureClassification | None = None
    repair: RepairResult | None = None
    verification: VerificationResult | None = None
    ledger: CostLedger
    artifacts: RunArtifacts = Field(default_factory=RunArtifacts)
    workflow_id: str | None = None  # AgentField workflow ID for DAG lookup
    vc_id: str | None = None  # Verifiable Credential ID
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ─── Agentic triage schemas ───────────────────────────────────────────────


class InvestigationAction(BaseModel):
    """A concrete action the triage agent wants to take to resolve uncertainty."""

    action: Literal["read_file", "grep", "git_log", "list_dir"]
    target: str  # file path, grep pattern, or directory
    reason: str  # why this would help resolve uncertainty


class TriageHypothesis(BaseModel):
    """One reasoning step's output from the triage agent.

    The agent MUST self-report confidence and what would help it be more
    confident. This enforces the 'search before guess' principle.
    """

    failure_type: FailureType
    confidence: float = Field(..., ge=0.0, le=1.0)
    root_cause: str  # narrative explanation
    evidence: list[str] = Field(default_factory=list)
    likely_files: list[str] = Field(default_factory=list)
    confidence_reasoning: str  # "I'm X because..."
    needs_investigation: list[InvestigationAction] = Field(default_factory=list)
    can_proceed: bool  # agent's own opinion on whether confidence is sufficient


class AgentBudget(BaseModel):
    """Per-agent budget tracking with hard/soft caps."""

    max_cost_usd: float = 0.02
    max_iterations: int = 5
    spent_usd: float = 0.0
    iterations: int = 0

    def can_continue(self) -> tuple[bool, str | None]:
        """Check if agent can continue. Returns (ok, reason_if_blocked)."""
        if self.iterations >= self.max_iterations:
            return False, f"iteration cap ({self.max_iterations})"
        if self.spent_usd >= self.max_cost_usd:
            return False, f"cost cap (${self.max_cost_usd:.3f})"
        return True, None

    def at_soft_limit(self) -> bool:
        """True if at 80% of budget — agent should justify continuing."""
        return self.spent_usd >= self.max_cost_usd * 0.8


class ClassifyResult(BaseModel):
    """Full output of triage-agent.classify."""

    classification: FailureClassification
    redaction: RedactionResult
    policy: PolicyConfig
    policy_violation: str | None = None
    proceed_to_repair: bool
    investigation_log: list[str] = Field(default_factory=list)
    root_cause: str = ""
    budget_used_usd: float = 0.0
    iterations: int = 0


# ─── Agentic repair schemas ──────────────────────────────────────────────


class HarnessRepairResult(BaseModel):
    """Schema passed to app.harness() for structured output from coding agent."""

    success: bool
    files_changed: list[str] = Field(default_factory=list)
    summary: str
    iterations_used: int = 0
