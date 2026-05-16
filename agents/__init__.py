"""PatchPilot agents.

Four agents coordinated via AgentField's app.call():

    triage  → classify failure, redact secrets, enforce policy
    repair  → generate + apply patch via app.ai()
    verify  → run command, collect diff, check forbidden paths
    audit   → emit Verifiable Credential, build summary

Each module has a `build_app()` factory and a `__main__` block for
docker-compose to run as `python -m agents.<name>`.
"""
