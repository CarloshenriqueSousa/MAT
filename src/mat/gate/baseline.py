"""Baseline measurer: captures test passing counts, coverage, and lint status before changes."""

from __future__ import annotations

import json
import re
from pathlib import Path

from mat.models.contracts import Baseline
from mat.sandbox.docker_runner import DockerRunner


class BaselineMeasurer:
    def __init__(self, runner: DockerRunner) -> None:
        self.runner = runner

    async def measure(self, run_id: str, worktree_path: Path) -> Baseline:
        """Executa pytest e ruff no container sandbox para coletar métricas baseline."""
        pytest_res = await self.runner.run_pytest(
            worktree_path, extra_args=["-v", "--cov=.", "--cov-report=json:/tmp/cov.json"]
        )
        ruff_res = await self.runner.run_ruff(worktree_path)

        # Parse de testes passados
        passed_match = re.search(r"(\d+) passed", pytest_res.stdout)
        total_passed = int(passed_match.group(1)) if passed_match else 0

        total_match = re.search(r"collected (\d+) items", pytest_res.stdout)
        total_tests = int(total_match.group(1)) if total_match else total_passed

        # Parse de cobertura do relatório gerado se existir
        coverage_pct = 0.0
        cov_file = worktree_path / "coverage.json"
        if cov_file.exists():
            try:
                data = json.loads(cov_file.read_text(encoding="utf-8"))
                coverage_pct = float(data.get("totals", {}).get("percent_covered", 0.0))
            except (json.JSONDecodeError, OSError, ValueError, KeyError):
                coverage_pct = 0.0
        else:
            # Fallback regex no stdout
            cov_match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", pytest_res.stdout)
            if cov_match:
                coverage_pct = float(cov_match.group(1))

        return Baseline(
            id=f"base_{run_id}",
            run_id=run_id,
            pytest_passed=total_passed,
            pytest_total=total_tests,
            coverage_pct=coverage_pct,
            ruff_clean=ruff_res.passed,
            raw_output=pytest_res.stdout,
        )
