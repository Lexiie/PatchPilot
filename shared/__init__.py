"""Shared domain logic for PatchPilot agents.

Modules:
    classifier: Failure classification by pattern matching
    redactor: Secret redaction before model calls
    policy: .patchpilot.yml loader and enforcement
    prompts: System prompts for app.ai() calls
    models: Pydantic schemas for inter-agent communication
    github: gh CLI wrapper utilities
"""
