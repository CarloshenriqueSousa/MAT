"""Shared pytest fixtures for the MAT test-suite."""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mat.agents.llm import LLMClient, LLMResponse
from mat.db import Database
from mat.project import open_project
from mat.sandbox.docker_runner import CommandResult
from mat.settings import (
    AgentProfile,
    AppConfig,
    GateConfig,
    LimitsConfig,
    MatConfig,
    PathsConfig,
    SandboxConfig,
)


@pytest.fixture
async def tmp_db(tmp_path: Path) -> AsyncIterator[Database]:
    """Create a temporary Database, initialise the schema, yield it, then close."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    await db.init_schema()
    yield db
    await db.close()


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    """Return a test AppConfig with temporary paths."""
    return AppConfig(
        mat=MatConfig(
            paths=PathsConfig(
                db=str(tmp_path / "mat.db"),
                worktrees=str(tmp_path / "worktrees"),
                artifacts=str(tmp_path / "artifacts"),
            ),
            limits=LimitsConfig(),
            sandbox=SandboxConfig(),
            gate=GateConfig(),
        ),
        agents={
            "analyst": AgentProfile(
                model="claude-opus-5-5",
                temperature=0.2,
                max_tokens=4096,
            ),
            "programmer": AgentProfile(
                model="claude-opus-5-5",
                temperature=0.3,
                max_tokens=8192,
            ),
        },
    )


# ── Repositório-alvo e CLI isolada ──────────────────────────────────────────


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "alvo"
    path.mkdir()
    git(path, "init", "--quiet", "-b", "main")
    git(path, "config", "user.name", "MAT tests")
    git(path, "config", "user.email", "mat-tests@localhost")
    (path / "app.py").write_text("print('v1')\n", encoding="utf-8", newline="\n")
    (path / "old.txt").write_text("velho\n", encoding="utf-8", newline="\n")
    git(path, "add", "--all")
    git(path, "commit", "--quiet", "-m", "inicial")
    return open_project(path)


@pytest.fixture
def mat_config(tmp_path, monkeypatch) -> Path:
    """Isola banco, worktrees e perfis de agentes da CLI no tmp_path."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "mat.yaml").write_text(
        "paths:\n"
        f"  db: '{(tmp_path / 'mat.db').as_posix()}'\n"
        f"  worktrees: '{(tmp_path / 'worktrees').as_posix()}'\n",
        encoding="utf-8",
    )
    (config_dir / "agents.yaml").write_text(
        "agents:\n"
        + "".join(
            f"  {role}:\n    model: 'modelo-{role}'\n    temperature: 0.2\n"
            "    max_tokens: 1000\n"
            for role in ("analyst", "programmer", "reviewer")
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAT_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


# ── Dublês do Analista real: LLM e sandbox ──────────────────────────────────

PLAN_JSON = {
    "approach": "Adicionar a rota /ping em app.py e um teste [bold]sem markup[/bold].",
    "risk_level": "low",
    "impacted_files": ["app.py", "tests/test_ping.py"],
    "test_scenarios": [
        {"name": "ping_ok", "type": "positive", "description": "responde pong"}
    ],
    "is_ambiguous": False,
}


@dataclass
class TextBlock:
    text: str
    type: str = "text"


def llm_answering(payload: dict) -> AsyncMock:
    llm = AsyncMock(spec=LLMClient)
    llm.call.return_value = LLMResponse(
        content=[TextBlock(json.dumps(payload))],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=10,
        raw_message=None,
    )
    return llm


@pytest.fixture
def fake_llm() -> AsyncMock:
    """LLM que responde um plano válido."""
    return llm_answering(PLAN_JSON)


@pytest.fixture
def fake_runner() -> AsyncMock:
    """Sandbox que mede um baseline verde sem Docker."""
    runner = AsyncMock()
    runner.run_pytest.return_value = CommandResult(
        exit_code=0, stdout="collected 2 items\n2 passed in 0.01s\n", stderr=""
    )
    runner.run_ruff.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    return runner
