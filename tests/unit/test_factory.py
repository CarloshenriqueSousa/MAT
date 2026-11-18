"""Tests for the agent factory (composition root)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mat.agents.analyst import AnalystAgent
from mat.agents.llm import LLMClient
from mat.agents.programmer import ProgrammerAgent
from mat.agents.reviewer import ReviewerAgent
from mat.agents.stubs import GateStub
from mat.agents.testers.happy_path import TesterHappyAgent
from mat.agents.verifier import VerifierAgent
from mat.db.connection import Database
from mat.engine.orchestrator import PipelineOrchestrator
from mat.engine.state_machine import ExecutionMode, RunStatus
from mat.factory import (
    REQUIRED_AGENT_PROFILES,
    AgentConfigError,
    build_real_agents,
    build_simulation_agents,
)
from mat.gate.deterministic import DeterministicGate
from mat.sandbox.docker_runner import DockerRunner
from mat.settings import AgentProfile, AppConfig, MatConfig, SandboxConfig, load_config

BASE_SHA = "a" * 40


def _config(tmp_path, *, agents: tuple[str, ...] = REQUIRED_AGENT_PROFILES) -> AppConfig:
    mat_cfg = MatConfig(sandbox=SandboxConfig(image="img:test", pids_limit=42))
    mat_cfg.paths.worktrees = str(tmp_path / "worktrees")
    return AppConfig(
        mat=mat_cfg,
        agents={
            name: AgentProfile(model=f"model-{name}", temperature=0.1, max_tokens=100)
            for name in agents
        },
    )


@pytest.fixture
def llm():
    return AsyncMock(spec=LLMClient)


def test_simulation_set_is_marked_as_simulation():
    agents = build_simulation_agents()

    assert agents.execution_mode is ExecutionMode.SIMULATION
    assert isinstance(agents.gate, GateStub)


def test_real_set_wires_each_role_with_its_own_profile(tmp_path, llm):
    config = _config(tmp_path)
    agents = build_real_agents(config, worktree_path=tmp_path, base_sha=BASE_SHA, llm=llm)

    assert agents.execution_mode is ExecutionMode.REAL
    assert isinstance(agents.analyst, AnalystAgent)
    assert isinstance(agents.programmer, ProgrammerAgent)
    assert isinstance(agents.reviewer, ReviewerAgent)
    assert isinstance(agents.tester_happy, TesterHappyAgent)
    assert isinstance(agents.verifier, VerifierAgent)
    assert isinstance(agents.gate, DeterministicGate)

    roles = {
        "analyst": agents.analyst,
        "programmer": agents.programmer,
        "reviewer": agents.reviewer,
        "tester_happy": agents.tester_happy,
        "verifier": agents.verifier,
    }
    for name, agent in roles.items():
        assert agent.profile.model == f"model-{name}"
    assert agents.programmer.base_sha == BASE_SHA
    assert agents.gate.worktree_path == tmp_path


def test_real_set_shares_one_runner_built_from_sandbox_config(tmp_path, llm):
    agents = build_real_agents(
        _config(tmp_path), worktree_path=tmp_path, base_sha=BASE_SHA, llm=llm
    )

    runners = {
        id(agents.programmer.runner),
        id(agents.reviewer.runner),
        id(agents.tester_happy.runner),
        id(agents.gate.runner),
        id(agents.analyst.measurer.runner),
    }
    assert len(runners) == 1
    runner = agents.gate.runner
    assert isinstance(runner, DockerRunner)
    assert (runner.image, runner.pids_limit) == ("img:test", 42)


def test_missing_profiles_are_all_reported(tmp_path, llm):
    config = _config(tmp_path, agents=("analyst", "programmer"))

    with pytest.raises(AgentConfigError, match="reviewer, tester_happy, verifier"):
        build_real_agents(config, worktree_path=tmp_path, base_sha=BASE_SHA, llm=llm)


@pytest.mark.parametrize("base_sha", ["HEAD", "abc123", "A" * 40, ""])
def test_rejects_non_immutable_base_sha(tmp_path, llm, base_sha):
    with pytest.raises(AgentConfigError, match="base_sha"):
        build_real_agents(_config(tmp_path), worktree_path=tmp_path, base_sha=base_sha, llm=llm)


def test_rejects_missing_worktree(tmp_path, llm):
    with pytest.raises(AgentConfigError, match="Worktree"):
        build_real_agents(
            _config(tmp_path), worktree_path=tmp_path / "nope", base_sha=BASE_SHA, llm=llm
        )


def test_requires_api_key_when_no_client_is_injected(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(AgentConfigError, match="ANTHROPIC_API_KEY"):
        build_real_agents(_config(tmp_path), worktree_path=tmp_path, base_sha=BASE_SHA)


def test_shipped_agents_yaml_defines_every_required_profile():
    config = load_config()

    assert set(REQUIRED_AGENT_PROFILES) <= set(config.agents)


async def test_orchestrator_accepts_agent_set_and_records_its_mode(tmp_path):
    db = Database(tmp_path / "factory.db")
    await db.init_schema()
    agents = build_simulation_agents()

    orchestrator = PipelineOrchestrator(
        config=AppConfig(mat=MatConfig(), agents={}), db=db, **agents.orchestrator_kwargs()
    )
    status = await orchestrator.run("Somar números", run_id="run_factory")

    assert status == RunStatus.PASSED
    run = await db.fetch_one("SELECT execution_mode FROM runs WHERE id = 'run_factory';")
    assert run["execution_mode"] == "simulation"
    await db.close()
