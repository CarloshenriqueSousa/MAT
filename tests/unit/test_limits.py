"""Limites de tentativa/replan do MVP (D43/D55)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mat.agents.stubs import (
    AnalystStub,
    GateStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.db.connection import Database
from mat.engine.orchestrator import PipelineOrchestrator
from mat.engine.state_machine import RunStatus
from mat.settings import AppConfig, LimitsConfig, MatConfig, load_config


def test_defaults_follow_mvp_policy():
    limits = LimitsConfig()
    assert (limits.max_attempts_per_plan, limits.max_replans, limits.max_review_rounds) == (1, 0, 1)


def test_repository_config_keeps_mvp_defaults():
    limits = load_config().mat.limits
    assert (limits.max_attempts_per_plan, limits.max_replans) == (1, 0)


def test_opt_in_up_to_d11_ceiling_is_accepted():
    limits = LimitsConfig(max_attempts_per_plan=3, max_replans=2)
    assert (limits.max_attempts_per_plan, limits.max_replans) == (3, 2)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_attempts_per_plan": 0},
        {"max_attempts_per_plan": 4},
        {"max_replans": -1},
        {"max_replans": 3},
    ],
)
def test_limits_outside_ceiling_are_rejected(overrides):
    with pytest.raises(ValidationError):
        LimitsConfig(**overrides)


async def test_default_limits_stop_after_single_attempt(tmp_path):
    mat_cfg = MatConfig()
    mat_cfg.paths.db = str(tmp_path / "limits.db")
    config = AppConfig(mat=mat_cfg, agents={})
    db = Database(mat_cfg.paths.db)
    await db.init_schema()
    try:
        orchestrator = PipelineOrchestrator(
            config=config,
            db=db,
            analyst=AnalystStub(),
            programmer=ProgrammerStub(),
            reviewer=ReviewerStub(),
            tester_happy=TesterHappyStub(),
            verifier=VerifierStub(),
            gate=GateStub(passed=False),
        )

        status = await orchestrator.run("x", run_id="run_mvp")

        assert status == RunStatus.FAILED
        run = await db.fetch_one("SELECT plan_count, total_attempts FROM runs;")
        assert (run["plan_count"], run["total_attempts"]) == (1, 1)
    finally:
        await db.close()
