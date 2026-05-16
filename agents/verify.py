"""verify-agent — run verification commands and check policy.

This agent does NOT call any LLM. All steps are deterministic: shell out
to the verify command, read git diff, check forbidden paths.

Skills:
    run_command(command, repo_path) -> VerificationCommand
    collect_diff(repo_path) -> str
    list_changed_files(repo_path) -> list[str]
    check_paths(changed_files, forbidden_patterns) -> list[str]
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from agentfield import Agent

from shared.models import VerificationCommand, VerificationResult, VerificationStatus
from shared.policy import check_forbidden_paths


def build_app() -> Agent:
    """Construct the verify-agent (deterministic, no AI config needed)."""
    app = Agent(
        node_id="verify-agent",
        version="2.0.0",
        agentfield_server=os.getenv("AGENTFIELD_SERVER_URL", "http://localhost:8080"),
    )

    @app.skill(tags=["patchpilot", "verify"])
    async def run_verification(
        commands: list[str],
        repo_path: str,
    ) -> dict[str, Any]:
        """Run a list of verification commands sequentially.

        Args:
            commands: Shell commands to run (e.g. ["npm test"]).
            repo_path: Repo where commands run.

        Returns:
            VerificationResult dict with overall status and per-command details.
        """
        repo = Path(repo_path)
        results: list[VerificationCommand] = []
        all_passed = True
        any_passed = False

        for cmd in commands:
            start = time.perf_counter()
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=str(repo),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                exit_code = proc.returncode or 0
            except Exception as exc:  # noqa: BLE001
                exit_code = 1
                stdout = b""
                stderr = str(exc).encode()

            duration_ms = int((time.perf_counter() - start) * 1000)
            output_path = repo / f".patchpilot-verify-{int(time.time())}.log"
            output_path.write_bytes((stdout or b"") + b"\n" + (stderr or b""))

            results.append(
                VerificationCommand(
                    command=cmd,
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                    output_path=str(output_path),
                )
            )

            if exit_code == 0:
                any_passed = True
            else:
                all_passed = False

        status: VerificationStatus
        if all_passed and results:
            status = "verified_pass"
        elif any_passed:
            status = "partial_pass"
        else:
            status = "failed_after_patch"

        await app.note(
            f"Verification {status} ({len(results)} commands)",
            tags=["verify", status],
        )

        return VerificationResult(status=status, commands=results).model_dump()

    @app.skill(tags=["patchpilot", "verify"])
    async def collect_diff(repo_path: str) -> str:
        """Return `git diff` output for the working tree."""
        proc = await asyncio.create_subprocess_exec(
            "git", "diff",
            cwd=str(Path(repo_path)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode(errors="replace")

    @app.skill(tags=["patchpilot", "verify"])
    async def list_changed_files(repo_path: str) -> list[str]:
        """Return list of file paths modified in the working tree."""
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--name-only",
            cwd=str(Path(repo_path)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return [line for line in stdout.decode().split("\n") if line.strip()]

    @app.skill(tags=["patchpilot", "verify", "policy"])
    async def check_paths(
        changed_files: list[str],
        forbidden_patterns: list[str],
    ) -> list[str]:
        """Return any path-policy violations from the changed file list.

        Uses the same glob matcher as shared.policy.check_forbidden_paths,
        but exposed as an AgentField skill so it's traceable in the DAG.
        """
        from shared.models import PolicyConfig, PolicyRepairConfig

        # Build a stub policy with just the forbidden_paths we care about
        policy = PolicyConfig(repair=PolicyRepairConfig(forbidden_paths=forbidden_patterns))
        violations = check_forbidden_paths(changed_files, policy)
        if violations:
            await app.note(
                f"Path policy violations: {len(violations)}",
                tags=["verify", "policy", "violation"],
            )
        return violations

    return app


if __name__ == "__main__":
    build_app().run()
