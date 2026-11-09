"""Sandbox bash tool executor.

Runs bash commands strictly inside the Docker container with --network none.
"""

from __future__ import annotations

from pathlib import Path

from mat.sandbox.docker_runner import DockerRunner


class BashHandler:
    def __init__(self, worktree_root: Path, runner: DockerRunner) -> None:
        self.worktree_root = worktree_root
        self.runner = runner

    async def execute(self, command: str) -> str:
        """Executes bash command in Docker sandbox and formats the output."""
        res = await self.runner.run_command(self.worktree_root, ["bash", "-c", command])

        output_parts: list[str] = []
        if res.stdout:
            output_parts.append(res.stdout)
        if res.stderr:
            output_parts.append(f"[STDERR]\n{res.stderr}")
        if res.timed_out:
            output_parts.append("[TIMEOUT] Comando excedeu o tempo limite de execução.")

        output_parts.append(f"\n[Exit code: {res.exit_code}]")
        return "\n".join(output_parts)
