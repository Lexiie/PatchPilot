# Deploying PatchPilot to Zeabur

PatchPilot v2 ships as a Docker compose stack (control plane + 4 agents +
webhook + Postgres). Zeabur supports Docker compose deployments natively.

## Prereqs

- Zeabur account (sign up: https://zeabur.com)
- Hackathon credits — claim with code `BUILDER0516` at https://zeabur.com/events
- A GitHub repo that this PatchPilot deployment will service
- API keys for TokenRouter (and optionally Qwen Cloud)

## Quick deploy

1. **Push this repo to GitHub** (or fork from `Lexiie/PatchPilot`).
2. **Create a Zeabur project**, click "Deploy from GitHub", select the PatchPilot repo.
3. **Set environment variables** in Zeabur dashboard:
   - `TOKENROUTER_API_KEY`
   - `GITHUB_TOKEN`
   - `GITHUB_WEBHOOK_SECRET`
4. **Add Postgres** as a service — Zeabur provides managed Postgres, just click "Add Service".
5. **Wait for build + deploy** — Zeabur will run `docker compose up`.
6. **Configure GitHub webhook**:
   - Repo settings → Webhooks → Add webhook
   - URL: `https://<your-zeabur-domain>/webhooks/github`
   - Content type: `application/json`
   - Secret: same as `GITHUB_WEBHOOK_SECRET`
   - Events: `Workflow runs`

## Health check

Once deployed:

```bash
curl https://<your-zeabur-domain>:8000/health
# {"status": "ok"}

curl https://<your-zeabur-domain>:8080/api/v1/health
# AgentField control plane status
```

## Trigger a test repair

Push a commit that fails CI in the configured repo. Within seconds:

1. GitHub fires `workflow_run.completed` with `conclusion=failure`
2. Webhook receives, validates, enqueues async PatchPilot run
3. AgentField queues triage → repair → verify → audit
4. PR is opened (draft) with audit metadata

## Troubleshooting

- **Postgres connection errors**: Verify `DATABASE_URL` is set by Zeabur; the
  compose file expects it from the postgres service.
- **AgentField doesn't register agents**: Check `AGENTFIELD_SERVER_URL` is
  reachable from the agent containers (Zeabur's internal networking handles this).
- **Webhook signature validation fails**: Ensure `GITHUB_WEBHOOK_SECRET` in
  Zeabur matches what's set in GitHub repo's webhook config.

## Costs

PatchPilot's own infra cost on Zeabur (free hackathon tier):
- Control plane container: ~50MB RAM idle
- Agent containers (4): ~30MB RAM each
- Postgres: ~100MB RAM

Model API costs (TokenRouter):
- Free tier covers classification fallback
- Pro tier (~$0.02 per medium-risk repair) — hackathon credits cover this

Total for a 1-day hackathon: well within free credits.
