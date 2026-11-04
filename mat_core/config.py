from pathlib import Path
from typing import Literal
import yaml
from pydantic import BaseModel, Field


class SandboxConfig(BaseModel):
    image: str = "mat-sandbox:latest"
    network: str = "none"
    memory_limit: str = "2g"
    cpu_limit: float = 2.0
    timeout_seconds: int = 60


class LimitsConfig(BaseModel):
    max_attempts_per_plan: int = 3
    max_replans: int = 2
    token_budget_per_attempt: int = 80000
    timeout_seconds_per_attempt: int = 300

    @property
    def max_total_attempts(self) -> int:
        return (self.max_replans + 1) * self.max_attempts_per_plan


class GateConfig(BaseModel):
    require_zero_exit_code: bool = True
    require_ruff_clean: bool = True
    require_no_coverage_drop: bool = True


class PathsConfig(BaseModel):
    db: Path = Path("data/mat.db")
    worktrees: Path = Path("data/worktrees")
    artifacts: Path = Path("data/artifacts")


class MatPipelineConfig(BaseModel):
    name: str = "MAT Pipeline"
    version: str = "0.1.0"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    gate: GateConfig = Field(default_factory=GateConfig)


class AgentProfile(BaseModel):
    model: str
    temperature: float = 0.2
    max_tokens: int = 8000
    system_role: str


class AgentsConfig(BaseModel):
    agents: dict[str, AgentProfile]

    def get(self, agent_name: str) -> AgentProfile:
        if agent_name not in self.agents:
            raise KeyError(f"Agente '{agent_name}' não encontrado no agents.yaml")
        return self.agents[agent_name]


class AppConfig(BaseModel):
    mat: MatPipelineConfig
    agents: AgentsConfig


def load_config(
    mat_config_path: str | Path = "configs/mat.yaml",
    agents_config_path: str | Path = "configs/agents.yaml",
) -> AppConfig:
    mat_path = Path(mat_config_path)
    agents_path = Path(agents_config_path)

    if not mat_path.is_file():
        raise FileNotFoundError(f"Arquivo de config não encontrado: {mat_path}")
    if not agents_path.is_file():
        raise FileNotFoundError(f"Arquivo de config não encontrado: {agents_path}")

    with open(mat_path, "r", encoding="utf-8") as f:
        mat_raw = yaml.safe_load(f)
    with open(agents_path, "r", encoding="utf-8") as f:
        agents_raw = yaml.safe_load(f)

    return AppConfig(
        mat=MatPipelineConfig(**mat_raw.get("mat", {})),
        agents=AgentsConfig(**agents_raw),
    )
