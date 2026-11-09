"""Pydantic-based configuration loader for MAT.

Reads configs/mat.yaml and configs/agents.yaml and exposes typed config objects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ── mat.yaml models ──────────────────────────────────────────────────────────


class PathsConfig(BaseModel):
    """Filesystem paths used by the engine."""

    db: str = "data/mat.db"
    worktrees: str = "data/worktrees"
    artifacts: str = "data/artifacts"


class LimitsConfig(BaseModel):
    """Execution-budget limits."""

    max_attempts_per_plan: int = 3
    max_replans: int = 2
    token_budget_per_attempt: int = 80_000
    timeout_seconds_per_attempt: int = 300


class SandboxConfig(BaseModel):
    """Docker sandbox settings."""

    image: str = "mat-sandbox:latest"
    network: str = "none"
    memory_limit: str = "512m"
    cpu_limit: str = "1.0"
    timeout_seconds: int = 300


class GateConfig(BaseModel):
    """Quality-gate requirements."""

    require_zero_exit_code: bool = True
    require_ruff_clean: bool = True
    require_no_coverage_drop: bool = True


class MatConfig(BaseModel):
    """Top-level config loaded from configs/mat.yaml."""

    paths: PathsConfig = Field(default_factory=PathsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    gate: GateConfig = Field(default_factory=GateConfig)


# ── agents.yaml models ──────────────────────────────────────────────────────


class AgentProfile(BaseModel):
    """Configuration for a single agent role.

    Models are loaded from agents.yaml and NEVER hardcoded.
    """

    model: str
    temperature: float
    max_tokens: int
    system_role: str


# ── combined config ──────────────────────────────────────────────────────────


class AppConfig(BaseModel):
    """Application configuration combining mat.yaml and agents.yaml."""

    mat: MatConfig = Field(default_factory=MatConfig)
    agents: dict[str, AgentProfile] = Field(default_factory=dict)


# ── loader ───────────────────────────────────────────────────────────────────


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file and return its contents as a dict."""
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def load_config(
    *,
    mat_path: Path | None = None,
    agents_path: Path | None = None,
) -> AppConfig:
    """Load and validate the full application configuration.

    Parameters
    ----------
    mat_path:
        Override path to mat.yaml (defaults to ``<project>/configs/mat.yaml``).
    agents_path:
        Override path to agents.yaml (defaults to ``<project>/configs/agents.yaml``).
    """
    mat_file = mat_path or _PROJECT_ROOT / "configs" / "mat.yaml"
    agents_file = agents_path or _PROJECT_ROOT / "configs" / "agents.yaml"

    mat_data = _load_yaml(mat_file) if mat_file.exists() else {}
    agents_data = _load_yaml(agents_file) if agents_file.exists() else {}

    mat_cfg = MatConfig.model_validate(mat_data)

    raw_agents: dict[str, Any] = agents_data.get("agents", {})
    agent_profiles: dict[str, AgentProfile] = {
        name: AgentProfile.model_validate(profile) for name, profile in raw_agents.items()
    }

    return AppConfig(mat=mat_cfg, agents=agent_profiles)
