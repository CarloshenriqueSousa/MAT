import pytest
import sqlite3
from mat_core.db.connection import Database


@pytest.mark.asyncio
async def test_db_init_and_pragmas(tmp_path):
    db_file = tmp_path / "test.db"
    db = Database(db_file)
    await db.init_schema()

    # Verifica WAL mode
    journal_mode = await db.fetch_one("PRAGMA journal_mode;")
    assert journal_mode[0].lower() == "wal"

    # Verifica Foreign Keys
    fk = await db.fetch_one("PRAGMA foreign_keys;")
    assert fk[0] == 1

    await db.close()


@pytest.mark.asyncio
async def test_run_and_baseline_crud(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.init_schema()

    run_id = "run-001"
    await db.execute(
        "INSERT INTO runs (id, objective, status) VALUES (?, ?, ?)",
        (run_id, "Implementar feature de soma", "running"),
    )

    await db.execute(
        """
        INSERT INTO baselines (run_id, pytest_passed, pytest_total, coverage_pct, ruff_clean)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, 10, 10, 85.5, 1),
    )

    run = await db.fetch_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    assert run["objective"] == "Implementar feature de soma"
    assert run["status"] == "running"

    baseline = await db.fetch_one("SELECT * FROM baselines WHERE run_id = ?", (run_id,))
    assert baseline["pytest_passed"] == 10
    assert baseline["coverage_pct"] == 85.5
    assert baseline["ruff_clean"] == 1

    await db.close()


@pytest.mark.asyncio
async def test_normalized_task_deps(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.init_schema()

    run_id = "run-002"
    await db.execute("INSERT INTO runs (id, objective) VALUES (?, ?)", (run_id, "Test Deps"))

    # Cria duas tasks
    await db.execute(
        "INSERT INTO tasks (id, run_id, agent_type, phase) VALUES (?, ?, ?, ?)",
        ("task-1", run_id, "analyst", "analysis"),
    )
    await db.execute(
        "INSERT INTO tasks (id, run_id, agent_type, phase) VALUES (?, ?, ?, ?)",
        ("task-2", run_id, "programmer", "programming"),
    )

    # task-2 depende de task-1
    await db.execute(
        "INSERT INTO task_deps (task_id, depends_on_task_id) VALUES (?, ?)",
        ("task-2", "task-1"),
    )

    # Consulta dependências
    deps = await db.fetch_all(
        "SELECT depends_on_task_id FROM task_deps WHERE task_id = ?", ("task-2",)
    )
    assert len(deps) == 1
    assert deps[0]["depends_on_task_id"] == "task-1"

    await db.close()


@pytest.mark.asyncio
async def test_evidence_composite_pk(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.init_schema()

    ev_id = "ev_abc123"
    await db.execute("INSERT INTO runs (id, objective) VALUES (?, ?)", ("run-1", "Obj 1"))
    await db.execute("INSERT INTO runs (id, objective) VALUES (?, ?)", ("run-2", "Obj 2"))

    # Mesma evidência em runs diferentes: DEVE funcionar (sem colisão)
    await db.execute(
        "INSERT INTO evidence (evidence_id, run_id, agent_type, predicate, content) VALUES (?, ?, ?, ?, ?)",
        (ev_id, "run-1", "analyst", "file_scan", '{"files": 5}'),
    )
    await db.execute(
        "INSERT INTO evidence (evidence_id, run_id, agent_type, predicate, content) VALUES (?, ?, ?, ?, ?)",
        (ev_id, "run-2", "analyst", "file_scan", '{"files": 5}'),
    )

    # Mesma evidência na MESMA run: DEVE falhar (IntegrityError de PK)
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO evidence (evidence_id, run_id, agent_type, predicate, content) VALUES (?, ?, ?, ?, ?)",
            (ev_id, "run-1", "analyst", "file_scan", '{"files": 5}'),
        )

    await db.close()


@pytest.mark.asyncio
async def test_attempt_and_dispatch_lifecycle(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.init_schema()

    run_id = "run-003"
    await db.execute("INSERT INTO runs (id, objective) VALUES (?, ?)", (run_id, "Obj 3"))

    # Attempt 1
    attempt_id = "att-1"
    await db.execute(
        "INSERT INTO attempts (id, run_id, plan_index, attempt_index, token_budget) VALUES (?, ?, ?, ?, ?)",
        (attempt_id, run_id, 1, 1, 50000),
    )

    # Task e Dispatch
    task_id = "task-prog"
    await db.execute(
        "INSERT INTO tasks (id, run_id, agent_type, phase) VALUES (?, ?, ?, ?)",
        (task_id, run_id, "programmer", "programming"),
    )

    dispatch_id = "disp-1"
    await db.execute(
        "INSERT INTO dispatches (id, attempt_id, task_id, agent_type, tokens_used) VALUES (?, ?, ?, ?, ?)",
        (dispatch_id, attempt_id, task_id, "programmer", 1250),
    )

    disp = await db.fetch_one("SELECT * FROM dispatches WHERE id = ?", (dispatch_id,))
    assert disp["tokens_used"] == 1250
    assert disp["status"] == "dispatched"

    await db.close()
