"""GitHub webhook handler for workflow_run events.

Validates the signature, enqueues an async PatchPilot repair via
AgentField's `/api/v1/execute/async/` endpoint.

Deployment:
    docker compose up webhook
    or: uvicorn webhook.github:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="PatchPilot Webhook", version="2.0.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for orchestrators."""
    return {"status": "ok"}


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    x_github_event: str | None = Header(None, alias="X-GitHub-Event"),
) -> dict[str, Any]:
    """Receive GitHub workflow_run events and enqueue async repair.

    Filters:
        - X-GitHub-Event must be "workflow_run"
        - Body's action must be "completed"
        - Body's workflow_run.conclusion must be "failure"

    Returns immediately with a tracking ID. Actual repair runs
    asynchronously via AgentField.
    """
    # Read body raw (for signature validation) and parsed
    raw_body = await request.body()
    _verify_signature(raw_body, x_hub_signature_256)

    if x_github_event != "workflow_run":
        return {"status": "ignored", "reason": f"event={x_github_event}"}

    payload = await request.json()
    action = payload.get("action")
    if action != "completed":
        return {"status": "ignored", "reason": f"action={action}"}

    workflow_run = payload.get("workflow_run", {})
    conclusion = workflow_run.get("conclusion")
    if conclusion != "failure":
        return {"status": "ignored", "reason": f"conclusion={conclusion}"}

    repo = payload.get("repository", {}).get("full_name")
    run_id = workflow_run.get("id")
    if not repo or not run_id:
        raise HTTPException(status_code=400, detail="Missing repo or run_id in payload")

    # Enqueue via AgentField async execution
    af_url = os.getenv("AGENTFIELD_SERVER_URL", "http://localhost:8080")
    target = "triage-agent.classify"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{af_url}/api/v1/execute/async/{target}",
            json={
                "input": {
                    "github_repo": repo,
                    "github_run_id": str(run_id),
                    "trigger": "webhook",
                }
            },
        )
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"AgentField queue failed: {resp.status_code} {resp.text}",
            )
        execution = resp.json()

    return {
        "status": "queued",
        "execution_id": execution.get("execution_id"),
        "repo": repo,
        "run_id": run_id,
    }


def _verify_signature(body: bytes, header: str | None) -> None:
    """Validate the X-Hub-Signature-256 header.

    Raises HTTPException(401) if signature missing or mismatched.
    Skipped entirely if GITHUB_WEBHOOK_SECRET is not set (dev mode).
    """
    secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    if not secret:
        # Dev mode — no validation
        return
    if not header or not header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing or malformed signature")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise HTTPException(status_code=401, detail="Invalid signature")
