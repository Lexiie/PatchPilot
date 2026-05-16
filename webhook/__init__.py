"""GitHub webhook receiver — async repair entry point.

Run with:
    uvicorn webhook.github:app --host 0.0.0.0 --port 8000

GitHub repo settings → Webhooks → URL: https://<host>:8000/webhooks/github
Content-Type: application/json
Events: workflow_run

The handler validates the signature, filters for `workflow_run.completed`
with `conclusion=failure`, then enqueues a repair job via AgentField's
async execution endpoint.
"""
