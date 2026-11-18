"""Composition root: builds the set of agents the orchestrator runs with.

Two sets exist and both go through this module, so callers never wire agents by hand:

- ``build_simulation_agents``: stubs, recorded as ``execution_mode=simulation``.
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

    if not _FULL_SHA.fullmatch(base_sha):
        raise AgentConfigError("base_sha deve ser um SHA Git completo e imutável.")
    if not worktree_path.is_dir():
        raise AgentConfigError(f"Worktree não encontrado: {worktree_path}")

    if llm is None:
        llm = LLMClient()
        if llm.client is None:
            raise AgentConfigError(
                "ANTHROPIC_API_KEY não configurada; necessária para a execução real."
            )

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


def _require_profiles(config: AppConfig) -> dict[str, AgentProfile]:
    missing = [name for name in REQUIRED_AGENT_PROFILES if name not in config.agents]
    if missing:
        raise AgentConfigError(
            "Perfis ausentes em agents.yaml: " + ", ".join(missing) + "."
        )
    return {name: config.agents[name] for name in REQUIRED_AGENT_PROFILES}
