"""Deterministic Gate: Enforces strict exit codes, linting, coverage baseline, and anti-cheat guards."""

from __future__ import annotations

import re
from pathlib import Path

from mat.models.contracts import Baseline, DeterministicGateResult
from mat.sandbox.docker_runner import DockerRunner


class DeterministicGate:
    def __init__(self, runner: DockerRunner) -> None:
        self.runner = runner

    def check_anti_cheat(self, diff: str) -> str | None:
        """Verifica se o agente tentou trapacear o gate desabilitando testes ou cobertura."""
        # 1. Checa por adição de decorators de skip ou xfail
        if re.search(r"^\+\s*@pytest\.mark\.(skip|xfail)", diff, re.MULTILINE):
            return "Detecção de trapaça: diff adiciona @pytest.mark.skip ou @pytest.mark.xfail."

        # 2. Checa se alterou configurações de cobertura para reduzir exigência
        if re.search(r"^\+.*fail_under\s*=\s*[0-9]+", diff, re.MULTILINE):
            return "Detecção de trapaça: alteração não autorizada nas regras de cobertura."

        return None

    async def evaluate(
        self, worktree_path: Path, baseline: Baseline, diff: str = ""
    ) -> DeterministicGateResult:
        """Avalia deterministamente o código no container sandbox."""
        # 1. Guarda anti-cheat
        cheat_error = self.check_anti_cheat(diff)
        if cheat_error:
            return DeterministicGateResult(
                passed=False,
                error_message=cheat_error,
            )

        # 2. Executa linter ruff no container sandbox
        ruff_res = await self.runner.run_ruff(worktree_path)
        if not ruff_res.passed:
            return DeterministicGateResult(
                passed=False,
                ruff_clean=False,
                error_message=f"Ruff encontrou erros de linting:\n{ruff_res.stdout}\n{ruff_res.stderr}",
            )

        # 3. Executa pytest com cobertura no container sandbox
        pytest_res = await self.runner.run_pytest(
            worktree_path, extra_args=["-v", "--cov=.", "--cov-report=json:/tmp/cov.json"]
        )
        if not pytest_res.passed:
            return DeterministicGateResult(
                passed=False,
                ruff_clean=True,
                error_message=f"Pytest falhou com exit code {pytest_res.exit_code}:\n{pytest_res.stdout}\n{pytest_res.stderr}",
            )

        # Extrai contadores
        passed_match = re.search(r"(\d+) passed", pytest_res.stdout)
        total_passed = int(passed_match.group(1)) if passed_match else 0

        # Extrai cobertura
        coverage_pct = 0.0
        cov_match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", pytest_res.stdout)
        if cov_match:
            coverage_pct = float(cov_match.group(1))

        # 4. Checa regressão de cobertura em relação ao baseline
        if coverage_pct < baseline.coverage_pct:
            return DeterministicGateResult(
                passed=False,
                pytest_passed=total_passed,
                coverage_pct=coverage_pct,
                ruff_clean=True,
                error_message=f"Regressão de cobertura: era {baseline.coverage_pct}%, agora é {coverage_pct}%.",
            )

        return DeterministicGateResult(
            passed=True,
            pytest_passed=total_passed,
            coverage_pct=coverage_pct,
            ruff_clean=True,
        )
