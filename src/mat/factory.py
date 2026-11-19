"""Composition root: builds the set of agents the orchestrator runs with.

Three sets exist and all go through this module, so callers never wire agents by hand:

- ``build_simulation_agents``: stubs, recorded as ``execution_mode=simulation``.
- ``build_planning_agents``: the real Analyst over one worktree; every other role is
  ``NotWiredAgent``, so nothing can edit code. Recorded as ``execution_mode=planning``
  (D59).
- ``build_implementation_agents``: real Analyst, Programmer, Reviewer and deterministic
  gate over one worktree; Tester and Verifier are ``NotWiredAgent``. Recorded as
  ``execution_mode=implementation`` (D61).
- ``build_real_agents``: LLM agents + deterministic gate over one worktree, recorded as
  ``execution_mode=real``.

The execution mode travels with the agent set, so a run can never be recorded as
``real`` while using stubs (or the other way around).

This module is intentionally not imported by any package ``__init__`` to avoid the
``mat.agents`` <-> ``mat.engine`` import cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from mat.agents.analyst import AnalystAgent
from mat.agents.errors import AgentExecutionError
from mat.agents.llm import LLMClient
from mat.agents.programmer import ProgrammerAgent
from mat.agents.reviewer import ReviewerAgent
from mat.agents.stubs import (
    AnalystStub,
    GateStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.agents.testers.happy_path import TesterHappyAgent
from mat.agents.verifier import VerifierAgent
from mat.engine.state_machine import ExecutionMode
from mat.gate.baseline import BaselineMeasurer
from mat.gate.deterministic import DeterministicGate
from mat.sandbox.docker_runner import DockerRunner
from mat.sandbox.worktree import WorktreeManager
from mat.settings import AgentProfile, AppConfig

REQUIRED_AGENT_PROFILES: tuple[str, ...] = (
    "analyst",
    "programmer",
    "reviewer",
    "tester_happy",
    "verifier",
)

_FULL_SHA = re.compile(r"[0-9a-f]{40,64}")


class AgentConfigError(ValueError):
    """The configuration or environment cannot produce a valid agent set."""


@dataclass(frozen=True)
class AgentSet:
    """Everything ``PipelineOrchestrator`` needs besides config and database."""

    analyst: Any
    programmer: Any
    reviewer: Any
    tester_happy: Any
    verifier: Any
    gate: Any
    execution_mode: ExecutionMode

    def orchestrator_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``PipelineOrchestrator`` (shallow, no copies)."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


def build_simulation_agents() -> AgentSet:
    """Stub agents that walk the pipeline without touching any repository."""
    return AgentSet(
        analyst=AnalystStub(),
        programmer=ProgrammerStub(),
        reviewer=ReviewerStub(approve_on_turn=1),
        tester_happy=TesterHappyStub(passed=True),
        verifier=VerifierStub(approved=True),
        gate=GateStub(passed=True),
        execution_mode=ExecutionMode.SIMULATION,
    )


class NotWiredAgent:
    """Ocupa um papel que ainda não pode rodar neste modo (D59).

    Qualquer chamada falha sem retry; o orquestrador converte isso em pausa
    ``awaiting_human`` em vez de inventar um resultado.
    """

    def __init__(self, role: str, mode: ExecutionMode) -> None:
        self.role = role
        self.mode = mode

    def _refuse(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AgentExecutionError(
            f"O papel '{self.role}' não está ligado no modo '{self.mode.value}'."
        )

    analyze = implement = review = check = test = verify = _refuse


def create_llm_client() -> LLMClient:
    """Cliente Anthropic a partir do ambiente; falha cedo sem ``ANTHROPIC_API_KEY``."""
    llm = LLMClient()
    if llm.client is None:
        raise AgentConfigError(
            "ANTHROPIC_API_KEY não configurada; necessária para a execução real."
        )
    return llm


# Perfis de agents.yaml exigidos por modo com projeto.
PROJECT_MODE_PROFILES: dict[ExecutionMode, tuple[str, ...]] = {
    ExecutionMode.PLANNING: ("analyst",),
    ExecutionMode.IMPLEMENTATION: ("analyst", "programmer", "reviewer"),
}


def check_project_config(config: AppConfig, mode: ExecutionMode) -> None:
    """Confere os perfis que ``mode`` exige antes de criar qualquer worktree."""
    _require_profiles(config, PROJECT_MODE_PROFILES[mode])


def check_planning_config(config: AppConfig) -> None:
    """Confere o que o conjunto ``planning`` exige antes de criar qualquer worktree."""
    check_project_config(config, ExecutionMode.PLANNING)


def build_planning_agents(
    config: AppConfig,
    *,
    worktree_path: Path,
    base_sha: str,
    llm: LLMClient | None = None,
    runner: DockerRunner | None = None,
) -> AgentSet:
    """Analista real sobre o worktree; os demais papéis não estão ligados (D59).

    O Analista só lê arquivos e mede o baseline no sandbox; nenhum agente deste
    conjunto altera o worktree.
    """
    profiles = _require_profiles(config, ("analyst",))
    _check_workspace(worktree_path, base_sha)
    llm = llm or create_llm_client()
    runner = runner or DockerRunner.from_config(config.mat.sandbox)
    mode = ExecutionMode.PLANNING
    return AgentSet(
        analyst=AnalystAgent(
            llm=llm,
            profile=profiles["analyst"],
            worktree_path=worktree_path,
            baseline_measurer=BaselineMeasurer(runner),
        ),
        **_not_wired(mode, "programmer", "reviewer", "tester_happy", "verifier", "gate"),
        execution_mode=mode,
    )


def build_implementation_agents(
    config: AppConfig,
    *,
    worktree_path: Path,
    base_sha: str,
    llm: LLMClient | None = None,
    runner: DockerRunner | None = None,
    worktree_manager: WorktreeManager | None = None,
) -> AgentSet:
    """Analista, Programador, Corretor e gate reais; Tester e Verificador não (D61).

    O Programador edita apenas o worktree (sem commit/push); toda execução ocorre no
    sandbox. Depois do gate a run pausa para revisão humana.
    """
    mode = ExecutionMode.IMPLEMENTATION
    profiles = _require_profiles(config, PROJECT_MODE_PROFILES[mode])
    _check_workspace(worktree_path, base_sha)
    llm = llm or create_llm_client()
    runner = runner or DockerRunner.from_config(config.mat.sandbox)
    worktree_manager = worktree_manager or WorktreeManager(Path(config.mat.paths.worktrees))
    return AgentSet(
        analyst=AnalystAgent(
            llm=llm,
            profile=profiles["analyst"],
            worktree_path=worktree_path,
            baseline_measurer=BaselineMeasurer(runner),
        ),
        programmer=ProgrammerAgent(
            llm=llm,
            profile=profiles["programmer"],
            worktree_path=worktree_path,
            docker_runner=runner,
            worktree_manager=worktree_manager,
            base_sha=base_sha,
        ),
        reviewer=ReviewerAgent(
            llm=llm,
            profile=profiles["reviewer"],
            worktree_path=worktree_path,
            docker_runner=runner,
        ),
        gate=DeterministicGate(runner, worktree_path=worktree_path),
        **_not_wired(mode, "tester_happy", "verifier"),
        execution_mode=mode,
    )


def build_unwired_agents(mode: ExecutionMode) -> AgentSet:
    """Conjunto sem nenhum agente ligado, para decisões humanas que não chamam agentes.

    Usado para registrar a aprovação de um plano ``planning`` sem exigir chave de API
    nem Docker.
    """
    return AgentSet(
        **_not_wired(
            mode, "analyst", "programmer", "reviewer", "tester_happy", "verifier", "gate"
        ),
        execution_mode=mode,
    )


def _not_wired(mode: ExecutionMode, *roles: str) -> dict[str, NotWiredAgent]:
    return {role: NotWiredAgent(role, mode) for role in roles}


def _check_workspace(worktree_path: Path, base_sha: str) -> None:
    if not _FULL_SHA.fullmatch(base_sha):
        raise AgentConfigError("base_sha deve ser um SHA Git completo e imutável.")
    if not worktree_path.is_dir():
        raise AgentConfigError(f"Worktree não encontrado: {worktree_path}")


def build_real_agents(
    config: AppConfig,
    *,
    worktree_path: Path,
    base_sha: str,
    llm: LLMClient | None = None,
    runner: DockerRunner | None = None,
    worktree_manager: WorktreeManager | None = None,
) -> AgentSet:
    """Build the LLM agents and deterministic gate for one worktree.

    Fails fast (``AgentConfigError``) on anything that would otherwise only break
    mid-run: missing agent profiles, missing API key, a worktree that does not
    exist, or a base revision that is not a full immutable SHA.
    """
    profiles = _require_profiles(config)
    _check_workspace(worktree_path, base_sha)
    llm = llm or create_llm_client()

    runner = runner or DockerRunner.from_config(config.mat.sandbox)
    worktree_manager = worktree_manager or WorktreeManager(Path(config.mat.paths.worktrees))

    return AgentSet(
        analyst=AnalystAgent(
            llm=llm,
            profile=profiles["analyst"],
            worktree_path=worktree_path,
            baseline_measurer=BaselineMeasurer(runner),
        ),
        programmer=ProgrammerAgent(
            llm=llm,
            profile=profiles["programmer"],
            worktree_path=worktree_path,
            docker_runner=runner,
            worktree_manager=worktree_manager,
            base_sha=base_sha,
        ),
        reviewer=ReviewerAgent(
            llm=llm,
            profile=profiles["reviewer"],
            worktree_path=worktree_path,
            docker_runner=runner,
        ),
        tester_happy=TesterHappyAgent(
            llm=llm,
            profile=profiles["tester_happy"],
            worktree_path=worktree_path,
            docker_runner=runner,
        ),
        verifier=VerifierAgent(llm=llm, profile=profiles["verifier"]),
        gate=DeterministicGate(runner, worktree_path=worktree_path),
        execution_mode=ExecutionMode.REAL,
    )


def _require_profiles(
    config: AppConfig, names: tuple[str, ...] = REQUIRED_AGENT_PROFILES
) -> dict[str, AgentProfile]:
    missing = [name for name in names if name not in config.agents]
    if missing:
        raise AgentConfigError(
            "Perfis ausentes em agents.yaml: " + ", ".join(missing) + "."
        )
    return {name: config.agents[name] for name in names}
