"""Pydantic contracts for inter-agent communication and persistence."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TestScenario(BaseModel):
    name: str
    type: str  # positive, adversarial, destructive
    description: str = ""


class Plan(BaseModel):
    id: str
    run_id: str
    plan_index: int
    approach: str
    risk_level: str = "low"
    impacted_files: list[str] = Field(default_factory=list)
    test_scenarios: list[TestScenario] = Field(default_factory=list)


class Baseline(BaseModel):
    id: str
    run_id: str
    pytest_passed: int = 0
    pytest_total: int = 0
    coverage_pct: float = 0.0
    ruff_clean: bool = True
    raw_output: str = ""


class Implementation(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    test_files_added: list[str] = Field(default_factory=list)
    diff: str = ""


class ReviewResult(BaseModel):
    approved: bool
    issues: list[str] = Field(default_factory=list)
    meets_task_objective: bool = True
    feedback: str = ""


class DeterministicGateResult(BaseModel):
    passed: bool
    pytest_passed: int = 0
    pytest_total: int = 0
    coverage_pct: float = 0.0
    ruff_clean: bool = True
    error_message: str | None = None


class TestResult(BaseModel):
    agent_type: str
    passed: bool
    tests_run: int = 0
    tests_passed: int = 0
    raw_output: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class Verdict(BaseModel):
    approved: bool
    criteria: dict[str, bool] = Field(default_factory=dict)
    evidence_cited: list[str] = Field(default_factory=list)
    rationale: str = ""
