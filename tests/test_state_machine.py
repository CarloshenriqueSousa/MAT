import pytest
from mat_agents.stubs import (
    AnalystStub,
    ConsolidatorStub,
    ProgrammerStub,
    TesterBypassStub,
    TesterBreakerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat_core.config import AppConfig, MatPipelineConfig, AgentsConfig
from mat_core.db.connection import Database
from mat_core.state_machine.engine import StateMachineEngine
from mat_core.state_machine.states import Phase, RunStatus


def make_config(tmp_path) -> AppConfig:
    mat_cfg = MatPipelineConfig()
    mat_cfg.paths.db = tmp_path / "test_sm.db"
    mat_cfg.paths.worktrees = tmp_path / "worktrees"
    mat_cfg.limits.max_attempts_per_plan = 3
    mat_cfg.limits.max_replans = 1  # 2 planos no total x 3 tentativas = 6 tentativas
    return AppConfig(mat=mat_cfg, agents=AgentsConfig(agents={}))


@pytest.mark.asyncio
async def test_state_machine_happy_path(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.mat.paths.db)
    await db.init_schema()

    engine = StateMachineEngine(
        config=config,
        db=db,
        analyst_agent=AnalystStub(),
        programmer_agent=ProgrammerStub(should_fail=False),
        tester_happy=TesterHappyStub(passed=True),
        tester_bypass=TesterBypassStub(bypass_prevented=True),
        tester_breaker=TesterBreakerStub(resilience_passed=True),
        tester_consolidator=ConsolidatorStub(),
        verifier_agent=VerifierStub(passes_gate=True),
    )

    ctx = await engine.run("Implementar soma")

    assert ctx.status == RunStatus.COMPLETED
    assert ctx.current_phase == Phase.COMPLETED
    assert ctx.plan_index == 1
    assert ctx.attempt_index == 1
    assert ctx.total_attempts == 1

    # Verifica se gravou no banco
    run_row = await db.fetch_one("SELECT * FROM runs WHERE id = ?", (ctx.run_id,))
    assert run_row["status"] == "completed"

    baseline_row = await db.fetch_one("SELECT * FROM baselines WHERE run_id = ?", (ctx.run_id,))
    assert baseline_row is not None
    assert baseline_row["pytest_passed"] == 5

    attempts = await db.fetch_all("SELECT * FROM attempts WHERE run_id = ?", (ctx.run_id,))
    assert len(attempts) == 1
    assert attempts[0]["status"] == "passed"

    await db.close()


@pytest.mark.asyncio
async def test_state_machine_retry_within_same_plan(tmp_path):
    """Tentativa 1 falha no Verificador; Tentativa 2 passa."""
    config = make_config(tmp_path)
    db = Database(config.mat.paths.db)
    await db.init_schema()

    # Verificador que falha na 1ª vez e passa na 2ª
    class TwoStepVerifier:
        def __init__(self):
            self.call_count = 0

        async def execute(self, ctx, consolidated_res):
            self.call_count += 1
            if self.call_count == 1:
                return await VerifierStub(passes_gate=False, reason="Falta cobertura").execute(ctx, consolidated_res)
            return await VerifierStub(passes_gate=True).execute(ctx, consolidated_res)

    engine = StateMachineEngine(
        config=config,
        db=db,
        analyst_agent=AnalystStub(),
        programmer_agent=ProgrammerStub(should_fail=False),
        tester_happy=TesterHappyStub(passed=True),
        tester_bypass=TesterBypassStub(bypass_prevented=True),
        tester_breaker=TesterBreakerStub(resilience_passed=True),
        tester_consolidator=ConsolidatorStub(),
        verifier_agent=TwoStepVerifier(),
    )

    ctx = await engine.run("Implementar autenticação")

    assert ctx.status == RunStatus.COMPLETED
    assert ctx.plan_index == 1
    assert ctx.attempt_index == 2  # Passou na 2ª tentativa!
    assert ctx.total_attempts == 2

    attempts = await db.fetch_all("SELECT * FROM attempts WHERE run_id = ? ORDER BY started_at", (ctx.run_id,))
    assert len(attempts) == 2
    assert attempts[0]["status"] == "failed"
    assert attempts[1]["status"] == "passed"

    await db.close()


@pytest.mark.asyncio
async def test_state_machine_escalation_to_analyst(tmp_path):
    """3 tentativas falham no plano 1; máquina escala para o Analista (Plano 2); passa no plano 2."""
    config = make_config(tmp_path)
    db = Database(config.mat.paths.db)
    await db.init_schema()

    class EscalatingVerifier:
        async def execute(self, ctx, consolidated_res):
            # Falha se for plano 1, passa se for plano 2
            if ctx.plan_index == 1:
                return await VerifierStub(passes_gate=False, reason="Plano 1 inviável").execute(ctx, consolidated_res)
            return await VerifierStub(passes_gate=True).execute(ctx, consolidated_res)

    engine = StateMachineEngine(
        config=config,
        db=db,
        analyst_agent=AnalystStub(),
        programmer_agent=ProgrammerStub(should_fail=False),
        tester_happy=TesterHappyStub(passed=True),
        tester_bypass=TesterBypassStub(bypass_prevented=True),
        tester_breaker=TesterBreakerStub(resilience_passed=True),
        tester_consolidator=ConsolidatorStub(),
        verifier_agent=EscalatingVerifier(),
    )

    ctx = await engine.run("Refatorar módulo legado")

    assert ctx.status == RunStatus.COMPLETED
    assert ctx.plan_index == 2      # Foi para o Plano 2!
    assert ctx.attempt_index == 1   # Passou na 1ª tentativa do Plano 2
    assert ctx.total_attempts == 4  # 3 no plano 1 + 1 no plano 2

    plans = await db.fetch_all(
        "SELECT DISTINCT plan_index FROM attempts WHERE run_id = ?", (ctx.run_id,)
    )
    assert len(plans) == 2

    await db.close()


@pytest.mark.asyncio
async def test_state_machine_hard_limit_exhaustion(tmp_path):
    """Todas as tentativas em todos os planos falham; termina em FAILED."""
    config = make_config(tmp_path)
    config.mat.limits.max_attempts_per_plan = 2
    config.mat.limits.max_replans = 1  # 2 planos x 2 tentativas = 4 tentativas máx
    db = Database(config.mat.paths.db)
    await db.init_schema()

    engine = StateMachineEngine(
        config=config,
        db=db,
        analyst_agent=AnalystStub(),
        programmer_agent=ProgrammerStub(should_fail=False),
        tester_happy=TesterHappyStub(passed=True),
        tester_bypass=TesterBypassStub(bypass_prevented=True),
        tester_breaker=TesterBreakerStub(resilience_passed=True),
        tester_consolidator=ConsolidatorStub(),
        verifier_agent=VerifierStub(passes_gate=False, reason="Impossível aprovar"),
    )

    ctx = await engine.run("Task impossível")

    assert ctx.status == RunStatus.FAILED
    assert ctx.current_phase == Phase.FAILED
    assert ctx.total_attempts == 4

    run_row = await db.fetch_one("SELECT * FROM runs WHERE id = ?", (ctx.run_id,))
    assert run_row["status"] == "failed"

    await db.close()
