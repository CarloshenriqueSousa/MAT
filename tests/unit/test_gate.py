"""Unit tests for DeterministicGate and anti-cheat mechanisms."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mat.gate.deterministic import DeterministicGate
from mat.models.contracts import Baseline
from mat.sandbox.docker_runner import CommandResult, DockerRunner


def test_anti_cheat_detects_skip_decorator():
    gate = DeterministicGate(runner=AsyncMock())
    diff = """
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -10,3 +10,4 @@
+@pytest.mark.skip(reason="cheating")
 def test_broken():
     assert False
"""
    error = gate.check_anti_cheat(diff)
    assert error is not None
    assert "Detecção de trapaça" in error


def test_anti_cheat_detects_xfail_decorator():
    gate = DeterministicGate(runner=AsyncMock())
    diff = """
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -10,3 +10,4 @@
+@pytest.mark.xfail
 def test_broken():
     assert False
"""
    error = gate.check_anti_cheat(diff)
    assert error is not None
    assert "Detecção de trapaça" in error


@pytest.mark.asyncio
async def test_deterministic_gate_passes_when_clean(tmp_path):
    mock_runner = AsyncMock(spec=DockerRunner)
    mock_runner.run_ruff.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    mock_runner.run_pytest.return_value = CommandResult(
        exit_code=0, stdout="TOTAL 10 0 100%\n5 passed in 0.05s", stderr=""
    )

    gate = DeterministicGate(runner=mock_runner)
    baseline = Baseline(
        id="base_1",
        run_id="run_1",
        pytest_passed=5,
        pytest_total=5,
        coverage_pct=80.0,
        ruff_clean=True,
    )

    res = await gate.evaluate(tmp_path, baseline, diff="")
    assert res.passed
    assert res.ruff_clean
    assert res.coverage_pct == 100.0


@pytest.mark.asyncio
async def test_deterministic_gate_fails_on_coverage_regression(tmp_path):
    mock_runner = AsyncMock(spec=DockerRunner)
    mock_runner.run_ruff.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    mock_runner.run_pytest.return_value = CommandResult(
        exit_code=0, stdout="TOTAL 10 5 50%\n5 passed in 0.05s", stderr=""
    )

    gate = DeterministicGate(runner=mock_runner)
    baseline = Baseline(
        id="base_1",
        run_id="run_1",
        pytest_passed=5,
        pytest_total=5,
        coverage_pct=85.0,  # Baseline era 85%, agora deu 50%
        ruff_clean=True,
    )

    res = await gate.evaluate(tmp_path, baseline, diff="")
    assert not res.passed
    assert "Regressão de cobertura" in (res.error_message or "")
