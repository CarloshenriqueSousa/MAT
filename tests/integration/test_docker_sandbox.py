"""Integration tests for the Docker sandbox runner."""

from __future__ import annotations

import pytest

from mat.sandbox.docker_runner import DockerRunner


@pytest.mark.asyncio
async def test_docker_runner_pytest_in_sandbox(tmp_path):
    runner = DockerRunner(image="mat-sandbox:latest")

    # Cria mini projeto com código e teste
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()
    (src_dir / "__init__.py").touch()

    (src_dir / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
    )
    (tests_dir / "test_calculator.py").write_text(
        "from src.calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    result = await runner.run_pytest(tmp_path)
    assert result.passed, (
        f"Pytest falhou no sandbox:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in result.stdout


@pytest.mark.asyncio
async def test_docker_runner_network_none_blocks_egress(tmp_path):
    runner = DockerRunner(image="mat-sandbox:latest")

    # Tenta abrir socket de rede externo no container (deve falhar por causa de --network none)
    cmd = [
        "python",
        "-c",
        "import socket; s = socket.socket(); s.settimeout(2); s.connect(('1.1.1.1', 80))",
    ]
    result = await runner.run_command(tmp_path, cmd)

    # Não deve conseguir conectar (exit code != 0)
    assert not result.passed
    assert "Network is unreachable" in result.stderr or result.exit_code != 0


@pytest.mark.asyncio
async def test_docker_runner_ruff_in_sandbox(tmp_path):
    runner = DockerRunner(image="mat-sandbox:latest")

    # Arquivo com erro de formatação/lint intencional
    bad_code = tmp_path / "bad.py"
    bad_code.write_text("import sys\nimport os\n\nx = 1\n", encoding="utf-8")

    result = await runner.run_ruff(tmp_path)
    # Ruff deve acusar imports não utilizados
    assert not result.passed
    assert "F401" in result.stdout or "unused import" in result.stdout.lower()
