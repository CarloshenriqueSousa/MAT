"""Sandbox bash tool executor.

Runs bash commands strictly inside the Docker container with --network none, after
checking them against the run's command policy (D64).
"""

from __future__ import annotations

from pathlib import Path

from mat.sandbox.docker_runner import DockerRunner
from mat.tools.command_policy import CommandApprovalRequired, CommandPolicy


class BashHandler:
    def __init__(
        self,
        worktree_root: Path,
        runner: DockerRunner,
        policy: CommandPolicy | None = None,
    ) -> None:
        self.worktree_root = worktree_root
        self.runner = runner
        self.policy = policy or CommandPolicy()

    async def execute(self, command: str) -> str:
        """Executes bash command in Docker sandbox and formats the output.

        Comandos recusados pela pessoa não rodam e o agente é avisado; comandos fora da
        política interrompem o agente com ``CommandApprovalRequired``.
        """
        normalized = command.strip()
        if normalized in self.policy.denied:
            return (
                "[NÃO EXECUTADO] A pessoa recusou este comando. Use os comandos de "
                "validação conhecidos (pytest, ruff) ou siga sem ele."
            )
        decision = self.policy.check(normalized)
        if not decision.allowed:
            raise CommandApprovalRequired(normalized, decision.reason)

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
