"""Shared pytest fixtures for the MAT test-suite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mat.db import Database
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
                system_role="Analista de teste",
            ),
            "programmer": AgentProfile(
                model="claude-opus-5-5",
                temperature=0.3,
                max_tokens=8192,
                system_role="Programador de teste",
            ),
        },
    )
