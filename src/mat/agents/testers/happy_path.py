"""Happy Path Tester Agent: executes and validates standard behavior in Docker sandbox."""

from __future__ import annotations

import re
from pathlib import Path

from mat.agents.llm import LLMClient
from mat.models.contracts import Implementation, Plan, TestResult
from mat.sandbox.docker_runner import DockerRunner
from mat.settings import AgentProfile


class TesterHappyAgent:
    __test__ = False  # Previne coleta indevida pelo pytest

    def __init__(
        self,
        llm: LLMClient,
        profile: AgentProfile,
        worktree_path: Path,
        docker_runner: DockerRunner,
    ) -> None:
        self.llm = llm
        self.profile = profile
        self.worktree_path = worktree_path
        self.runner = docker_runner

    async def test(self, plan: Plan, impl: Implementation) -> TestResult:
        """Executa a suíte de testes convencionais no sandbox Docker."""
        # Executa pytest com verbose dentro do container
        res = await self.runner.run_pytest(self.worktree_path, extra_args=["-v"])

        passed_match = re.search(r"(\d+) passed", res.stdout)
        total_passed = int(passed_match.group(1)) if passed_match else 0

        failed_match = re.search(r"(\d+) failed", res.stdout)
        total_failed = int(failed_match.group(1)) if failed_match else 0

        total_run = total_passed + total_failed
        all_passed = res.passed and total_failed == 0

        return TestResult(
            agent_type="tester_happy",
            passed=all_passed,
            tests_run=total_run,
            tests_passed=total_passed,
            raw_output=res.stdout,
            details={"exit_code": res.exit_code, "stderr": res.stderr},
        )
