# PatchPilot v2

> Audit-grade CI repair automation. Multi-agent. Cryptographically auditable.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![AgentField](https://img.shields.io/badge/AgentField-plugin-lightgrey)](https://agentfield.ai)
[![TokenRouter](https://img.shields.io/badge/TokenRouter-routed-lightgrey)](https://www.tokenrouter.com)
[![Tests](https://img.shields.io/badge/tests-70_passing-success)](./tests)

PatchPilot turns red CI into reviewed, locally verified repairs with a tamper-proof audit trail. Every step is signed. Every decision is policy-enforced. Every workflow is replayable.

Built for **Agent Forge AI Hackathon** on top of [AgentField](https://agentfield.ai/) (multi-agent control plane) + [TokenRouter](https://www.tokenrouter.com/) (model gateway) + [Qwen Cloud](https://www.qwencloud.com/) (coding-specialized models) + [Zeabur](https://zeabur.com/) (deployment).

---

## The problem

A $200/hour engineer opens GitHub Actions, scrolls 500 lines of logs, finds a lint error on line 12, fixes one character, pushes, waits 10 minutes for CI again.

Every team. Every day. The most expensive copy-paste in software.

AI coding agents (Claude Code, Codex) can fix these interactively. But they are **not safe for unattended CI automation**:

- ❌ No signed audit trail — compliance teams can't trust unsigned changes
- ❌ No enforceable policy — agent can patch your auth code or secrets
- ❌ No budget cap — agent might loop and burn $5 on a lint fix
- ❌ No multi-repo scale — each fix requires interactive setup
- ❌ No webhook-driven execution — can't trigger on CI failure events

PatchPilot is the missing layer: **autonomous, policy-enforced, audit-grade CI repair.**

---

## What it does

```bash
# Classify a failure (instant, free)
$ patchpilot diagnose --command "npm test"
  Type:         unit_test
  Confidence:   0.85
  Risk:         medium
  Likely files: src/parser.ts

# Full repair (agentic, budget-capped, locally verified)
$ patchpilot repair --command "npm test" --budget 0.25
  [triage]  Investigating... read tsconfig.json, src/parser.ts
  [triage]  Root cause: type narrowing broke parseFloat fallback
  [repair]  Planning fix... permission_mode=plan
  [repair]  Applied patch (2 iterations, $0.043)
  [verify]  npm test → 142 passing
  [audit]   VC signed, report generated
  ✓ Report: .patchpilot/runs/pp_KeH3Lm9Q/report.md

# GitHub Actions repair + draft PR
$ patchpilot repair-gh --repo owner/repo --run latest-failed --create-pr

# Triage only (no patching, $0)
$ patchpilot repair --command "npm test" --mode triage
```

Three modes: `full` (repair + verify + audit report), `triage` (classify only), `dry-run` (preview pipeline, zero cost). Draft PR creation is available through `repair-gh --create-pr`.

---

## Why not just prompt Claude Code?

| | Claude Code / Codex | PatchPilot |
|---|---|---|
| Fix a bug interactively | ✅ Great at this | Not the goal |
| Schedule on every failed CI run | ❌ | ✅ Webhook-driven |
| Enforce "never touch auth/**" | ❌ | ✅ `.patchpilot.yml` policy |
| Cryptographic proof of what ran | ❌ | ✅ Verifiable Credentials |
| Hard budget cap per run | ❌ | ✅ $0.02 triage, $0.20 repair |
| Investigate before guessing | ❌ (just generates) | ✅ Confidence-gated reasoning |
| Multi-repo, multi-tenant | ❌ | ✅ Control plane scales |
| Reject forbidden path changes | ❌ | ✅ Path policy check + repair-agent rollback |

**Claude Code is a coding assistant. PatchPilot is infrastructure for autonomous code maintenance.**

---

## How the agents reason

PatchPilot agents are **genuinely agentic** — they investigate, hypothesize, and self-correct. Not scripted workflows with an LLM call bolted on.

### triage-agent — iterative root cause analysis

```
┌─ Pattern match (free, instant) ─────────────────────────────────────┐
│  confidence >= 0.85?  YES → done, $0.00                            │
│                       NO  ↓                                         │
├─ Reasoning loop (max 5 iterations, $0.02 cap) ──────────────────────┤
│  1. Hypothesize: "What type? How confident? Why?"                   │
│  2. Self-report: "I need to read tsconfig.json to be sure"          │
│  3. Investigate: read files, grep, git log (agent decides what)     │
│  4. Re-hypothesize with new evidence                                │
│  5. Repeat until confident (≥0.75) OR budget exhausted              │
├─ Guardrails ────────────────────────────────────────────────────────┤
│  • Agent MUST report confidence_reasoning + needs_investigation     │
│  • If uncertain with no investigation path → escalate, don't guess  │
│  • Hard cap: 5 iterations, $0.02                                    │
│  • Soft warn at 80% budget                                          │
└─────────────────────────────────────────────────────────────────────┘
```

### repair-agent — multi-turn coding with plan-first

```
┌─ Confidence gate ───────────────────────────────────────────────────┐
│  classification.confidence < 0.6?  → decline repair, triage only    │
├─ Risk routing ──────────────────────────────────────────────────────┤
│  low risk  → cheap coding model, budget cap $0.05                  │
│  medium    → pro coding model, budget cap $0.20                    │
│  high      → never reaches here (policy blocks upstream)            │
├─ Harness execution (multi-turn coding agent) ───────────────────────┤
│  permission_mode="plan" → agent MUST plan before editing            │
│  1. Investigate repo (Read, Glob, Grep)                             │
│  2. Plan the fix                                                    │
│  3. Apply edits (Write, Edit)                                       │
│  4. Run verify command (Bash)                                       │
│  5. If fails → read error, iterate (up to max_turns)               │
│  6. If succeeds → return structured result                          │
├─ Post-execution guardrails ─────────────────────────────────────────┤
│  • Forbidden path check on all modified files                       │
│  • Violation? → rollback harness edits, reject repair               │
│  • Harness budget/turn cap enforced by AgentField itself            │
└─────────────────────────────────────────────────────────────────────┘
```

### verify-agent — deterministic (by design)

Verification IS deterministic. Running `npm test` doesn't need AI judgment. This agent stays a pure skill: run command, report exit code.

### audit-agent — lightweight judgment

Generates PR summary via cheap LLM call. Assesses review priority. Emits Verifiable Credential. Cross-run memory query for similar past fixes.

---

## Guardrails philosophy

> "Fully agentic, but bounded. When uncertain, investigate — never guess."

| Guardrail | Where | Effect |
|---|---|---|
| **Budget cap per agent** | triage ($0.02), repair ($0.05-0.20) | Hard stop when exhausted |
| **Iteration cap** | triage (5), repair (max_turns) | Prevents infinite loops |
| **Confidence gate** | repair entry | Won't attempt if classification weak |
| **Search-before-guess** | triage reasoning loop | Agent must list what would help before re-asking |
| **Plan-before-execute** | repair Harness `permission_mode="plan"` | Agent plans edits before making them |
| **Forbidden path rollback** | repair post-check | Rejects the repair and rolls back harness edits if forbidden files were touched |
| **Self-veto** | triage `can_proceed`, repair `success=false` | Agent can decline to proceed |
| **Soft warn at 80%** | triage budget | Logged for audit visibility |
| **Policy enforcement** | triage + verify | `.patchpilot.yml` rules enforced at infra level |

---

## Sponsor stack used

Every sponsor tool is **load-bearing, not decorative**.

| Sponsor | What we use it for | Where in code |
|---------|--------------------|---------------|
| **AgentField** | Multi-agent control plane, workflow DAG, DIDs/VCs, async exec, `app.harness()` for repair | `agents/*.py`, `docker-compose.yml` |
| **TokenRouter** | OpenAI-compatible model gateway, routes all `app.ai()` calls | `.env` `TOKENROUTER_BASE_URL` |
| **Qwen Cloud** | Coding-specialized models such as `qwen/qwen3-coder-next` for classification + repair | `PATCHPILOT_FREE_MODEL`, `PATCHPILOT_CHEAP_MODEL`, `PATCHPILOT_PRO_MODEL` |
| **Zeabur** | Deployment target (Docker compose), hosted Postgres for durable queue | `deployments/zeabur/` |

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        Clients                                     │
│   patchpilot CLI · GitHub webhook receiver · REST API consumers   │
└────────────────────────┬───────────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────────────┐
│                  AgentField Control Plane                          │
│   Routing · Memory · Policy gates · DAG tracking · VC issuance    │
└────────────────────────┬───────────────────────────────────────────┘
                         │
        ┌────────────────┼────────────────┐
        │                │                │
        ▼                ▼                ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ triage-agent │ │ repair-agent │ │ verify-agent │ │ audit-agent  │
└──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────────────┘
       │                │                │
       │  app.ai()      │  app.harness() │
       └────────────────┴────────────────┘
                        │
                        ▼
                ┌────────────────┐
                │  TokenRouter   │
                │ (OpenAI-compat)│
                └────────────────┘
```

---

## Supported language ecosystems

Pattern-based classifier covers 7 ecosystems out of the box. Languages
not in this list still work via the LLM-fallback classification path.

| Language | Tools detected | Categories covered |
|---|---|---|
| **TypeScript / JavaScript** | ESLint, Prettier, tsc, vitest, jest, mocha, cypress, playwright, npm/yarn/pnpm | lint, format, typecheck, unit_test, integration_test, dependency_config, package_lock, snapshot |
| **Python** | ruff, flake8, pylint, black, isort, mypy, pyright, pytest, poetry, uv | lint, format, typecheck, unit_test, dependency_config, package_lock |
| **Go** | go test, go vet, gofmt, goimports, golangci-lint, go.mod, go.sum | lint, format, typecheck, unit_test, dependency_config, package_lock, build_compile |
| **Rust** | cargo test, rustfmt, error[Exxxx], insta, Cargo.lock | typecheck, unit_test, format, dependency_config, build_compile, snapshot, package_lock |
| **Java** | javac, junit, maven [ERROR], gradle | typecheck, unit_test, dependency_config, build_compile |
| **Ruby** | rspec, rubocop, bundler, Gemfile.lock, capybara | lint, unit_test, dependency_config, package_lock, integration_test |
| **C / C++** | undefined reference, fatal error includes | build_compile |

Generic patterns also catch language-agnostic failures: network errors
(ETIMEDOUT, ECONNREFUSED, rate limit), missing env secrets, flaky test
markers, and CI infrastructure issues.

---

## Quick start

```bash
pip install -e ".[dev]"
patchpilot init        # creates .patchpilot.yml
cp .env.example .env   # fill in TOKENROUTER_API_KEY
patchpilot doctor      # verify environment
pytest                 # run local tests
patchpilot repair --command "npm test" --budget 0.25
```

---

## Run modes (deployment)

### CLI mode (no control plane required)

```bash
patchpilot repair --command "npm test"
```

Standalone. Outputs to `.patchpilot/runs/<run_id>/`.

### Server mode (AgentField + Docker compose)

```bash
docker compose up
# Control plane:        http://localhost:8080
# Webhook endpoint:     http://localhost:8000/webhooks/github
```

Full multi-agent setup with PostgreSQL durable queue, AgentField dashboard, and FastAPI webhook receiver.

---

## Audit trail

Every run produces:

```
.patchpilot/runs/<run_id>/
├── run.json              # Full PatchPilotRun (Pydantic-validated)
├── ledger.json           # Per-step cost ledger
├── report.md             # Markdown PR body
├── credentials.json      # Verifiable Credential (W3C standard)
└── redacted.log          # Secret-redacted version of failure log
```

When AgentField is running:
- Queryable workflow DAG at `/api/v1/workflows/<id>/dag`
- Cryptographically signed VC, verifiable offline via `af vc verify`

---

## Status

| Feature | Status |
|---|---|
| 7-step workflow pipeline | ✅ |
| 13 failure types × 7 languages classification | ✅ |
| Secret redaction (9 patterns) | ✅ |
| Policy enforcement (.patchpilot.yml) | ✅ |
| 3 modes (full / triage / dry-run) | ✅ |
| Standalone CLI orchestration | ✅ |
| 4-agent AgentField scaffolding | ✅ |
| Docker compose multi-service | ✅ |
| GitHub webhook receiver | ✅ |
| Risk-based tier routing | ✅ |
| Per-step cost ledger | ✅ |
| Markdown PR body generation | ✅ |
| 70 tests passing | ✅ |
| Live TokenRouter LLM repair verified (`qwen/qwen3-coder-next`) | ✅ |
| Live deployment (Zeabur) | ⬜ demo-day target |
| End-to-end demo with real LLM | ⬜ demo-day target |
| GitHub App integration | ⬜ post-hackathon |

---

## License

Apache-2.0. See [LICENSE](./LICENSE).
