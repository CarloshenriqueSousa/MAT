from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from mat_core.config import AppConfig
from mat_core.db.connection import Database
from mat_core.state_machine.states import Phase, RunStatus


@dataclass
class BaselineData:
    pytest_passed: int = 0
    pytest_total: int = 0
    coverage_pct: float = 0.0
    ruff_clean: bool = True
    raw_output: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionPlan:
    plan_id: str
    approach: str
    risk_level: str
    impacted_files: list[str] = field(default_factory=list)
    test_scenarios: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunContext:
    run_id: str
    objective: str
    db: Database
    config: AppConfig
    worktree_path: Path
    current_phase: Phase = Phase.ANALYSIS
    status: RunStatus = RunStatus.PENDING
    plan_index: int = 1
    attempt_index: int = 1
    total_attempts: int = 0
    current_attempt_id: str | None = None
    baseline: BaselineData | None = None
    current_plan: ExecutionPlan | None = None
    last_failure_reason: str | None = None
    tokens_used_in_attempt: int = 0
