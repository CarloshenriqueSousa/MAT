"""Pydantic contracts for inter-agent communication and persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class TestScenario(BaseModel):
    __test__ = False  # not a pytest test class
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


# pytest: 0 = tudo passou; 5 = nenhum teste coletado (base sem testes ainda é válida).
BASELINE_OK_PYTEST_EXIT_CODES = frozenset({0, 5})


class Baseline(BaseModel):
    id: str
    run_id: str
    pytest_passed: int = 0
    pytest_total: int = 0
    coverage_pct: float = 0.0
    ruff_clean: bool = True
    raw_output: str = ""
    # None quando o baseline não veio do sandbox (simulação ou registros antigos).
    pytest_exit_code: int | None = None
    pytest_timed_out: bool = False
    ruff_output: str = ""

    def failure_reasons(self) -> list[str]:
        """Por que esta base já falha antes de qualquer edição (D43); vazio se está ok.

        O gate exige pytest com saída 0 e Ruff limpo; uma base que já viola isso
        reprovaria toda tentativa por um problema que não é da tarefa.
        """
        reasons: list[str] = []
        if self.pytest_timed_out:
            reasons.append("Os testes existentes excederam o tempo limite do sandbox.")
        elif (
            self.pytest_exit_code is not None
            and self.pytest_exit_code not in BASELINE_OK_PYTEST_EXIT_CODES
        ):
            reasons.append(
                f"Os testes existentes falham na base (pytest saiu com {self.pytest_exit_code})."
            )
        if not self.ruff_clean:
            reasons.append("O Ruff já reporta erros na base.")
        return reasons


class Analysis(BaseModel):
    """Saída do Analista registrada no dispatch: plano e baseline medidos."""

    plan: Plan
    baseline: Baseline


class Workspace(BaseModel):
    """Onde uma solicitação trabalha (D58): branch própria num worktree isolado.

    ``path`` é None quando a run pausou antes de criar o worktree (checkout sujo).
    """

    repo_path: Path
    base_ref: str
    base_sha: str
    branch: str
    path: Path | None = None


class Implementation(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    test_files_added: list[str] = Field(default_factory=list)
    diff: str = ""


class ReviewResult(BaseModel):
    approved: bool
    issues: list[str] = Field(default_factory=list)
    meets_task_objective: bool = True
    feedback: str = ""


class TestFileChange(BaseModel):
    """Alteração num arquivo de teste que já existia na base (D42/D63)."""

    __test__ = False  # not a pytest test class
    path: str
    sha256: str  # hash da seção do diff; a aprovação vale só para esse conteúdo
    diff: str = ""  # trecho do diff para revisão humana (pode estar truncado)


class DeterministicGateResult(BaseModel):
    passed: bool
    pytest_passed: int = 0
    pytest_total: int = 0
    coverage_pct: float = 0.0
    ruff_clean: bool = True
    error_message: str | None = None
    # Testes existentes alterados sem aprovação; não vazio => o gate não rodou os testes.
    pending_test_changes: list[TestFileChange] = Field(default_factory=list)


class TestResult(BaseModel):
    __test__ = False  # not a pytest test class
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
