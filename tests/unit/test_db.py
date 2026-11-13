"""Unit tests for the MAT database layer."""

from __future__ import annotations

import uuid

import aiosqlite
import pytest

from mat.db import Database
from mat.db.repos.runs import RunsRepo

# ── helpers ──────────────────────────────────────────────────────────────────


def _uid() -> str:
    return uuid.uuid4().hex[:16]


# ── PRAGMA tests ─────────────────────────────────────────────────────────────


async def test_wal_mode_enabled(tmp_db: Database) -> None:
    """Writer connection must be in WAL journal mode."""
    row = await tmp_db.fetch_one("PRAGMA journal_mode")
    assert row is not None
    assert row[0] == "wal"


async def test_foreign_keys_enabled(tmp_db: Database) -> None:
    """Foreign-key enforcement must be ON."""
    row = await tmp_db.fetch_one("PRAGMA foreign_keys")
    assert row is not None
    assert row[0] == 1


# ── run + baseline CRUD ─────────────────────────────────────────────────────


async def test_create_run(tmp_db: Database) -> None:
    run_id = _uid()
    await tmp_db.execute(
        "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
        (run_id, "pending", "init"),
    )
    row = await tmp_db.fetch_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    assert row is not None
    assert row["status"] == "pending"
    assert row["base_sha"] is None
    assert row["plan_count"] == 0


async def test_legacy_runs_receive_unknown_execution_mode(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(db_path) as legacy_db:
        await legacy_db.execute(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending', 'running', 'passed', 'failed',
                        'awaiting_human', 'cancelled'
                    )),
                current_phase TEXT NOT NULL DEFAULT 'init',
                plan_count INTEGER NOT NULL DEFAULT 0,
                total_attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await legacy_db.execute(
            "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
            ("legacy-run", "passed", "completed"),
        )
        await legacy_db.commit()

    db = Database(db_path)
    await db.init_schema()
    run = await db.fetch_one(
        "SELECT execution_mode, base_sha FROM runs WHERE id = ?", ("legacy-run",)
    )
    assert run is not None
    assert run["execution_mode"] == "unknown"
    assert run["base_sha"] is None
    version = await db.fetch_one("PRAGMA user_version;")
    assert version is not None
    assert version[0] == 1
    await db.close()


async def test_runs_repo_persists_base_sha(tmp_db: Database) -> None:
    base_sha = "a" * 40
    run = await RunsRepo(tmp_db).create("run_with_base", base_sha=base_sha)
    assert run["base_sha"] == base_sha


async def test_run_status_check_constraint(tmp_db: Database) -> None:
    """Invalid status should be rejected."""
    with pytest.raises(aiosqlite.IntegrityError):
        await tmp_db.execute(
            "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
            (_uid(), "invalid_status", "init"),
        )


async def test_create_baseline(tmp_db: Database) -> None:
    run_id = _uid()
    baseline_id = _uid()
    await tmp_db.execute(
        "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
        (run_id, "pending", "init"),
    )
    await tmp_db.execute(
        "INSERT INTO baselines (id, run_id, pytest_passed, pytest_total, coverage_pct, ruff_clean, raw_output) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (baseline_id, run_id, 42, 50, 85.5, 1, "all passed"),
    )
    row = await tmp_db.fetch_one("SELECT * FROM baselines WHERE id = ?", (baseline_id,))
    assert row is not None
    assert row["pytest_passed"] == 42
    assert row["coverage_pct"] == pytest.approx(85.5)


# ── normalised task_deps ─────────────────────────────────────────────────────


async def test_task_deps_normalised(tmp_db: Database) -> None:
    run_id = _uid()
    t1, t2 = _uid(), _uid()
    await tmp_db.execute(
        "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
        (run_id, "running", "analysis"),
    )
    await tmp_db.execute(
        "INSERT INTO tasks (id, run_id, agent_type, phase) VALUES (?, ?, ?, ?)",
        (t1, run_id, "analyst", "analysis"),
    )
    await tmp_db.execute(
        "INSERT INTO tasks (id, run_id, agent_type, phase) VALUES (?, ?, ?, ?)",
        (t2, run_id, "programmer", "coding"),
    )
    await tmp_db.execute(
        "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
        (t2, t1),
    )

    # Duplicate should fail (composite PK)
    with pytest.raises(aiosqlite.IntegrityError):
        await tmp_db.execute(
            "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
            (t2, t1),
        )

    rows = await tmp_db.fetch_all("SELECT * FROM task_deps WHERE task_id = ?", (t2,))
    assert len(rows) == 1
    assert rows[0]["depends_on"] == t1


# ── evidence composite PK ───────────────────────────────────────────────────


async def test_evidence_same_hash_different_runs(tmp_db: Database) -> None:
    """Same evidence_id in different runs must succeed."""
    r1, r2 = _uid(), _uid()
    eid = "sha256-deadbeef"
    for rid in (r1, r2):
        await tmp_db.execute(
            "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
            (rid, "pending", "init"),
        )
    await tmp_db.execute(
        "INSERT INTO evidence (evidence_id, run_id, agent_type, predicate, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (eid, r1, "reviewer", "lint_clean", "ok"),
    )
    await tmp_db.execute(
        "INSERT INTO evidence (evidence_id, run_id, agent_type, predicate, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (eid, r2, "reviewer", "lint_clean", "ok"),
    )
    rows = await tmp_db.fetch_all("SELECT * FROM evidence WHERE evidence_id = ?", (eid,))
    assert len(rows) == 2


async def test_evidence_same_hash_same_run_fails(tmp_db: Database) -> None:
    """Duplicate (evidence_id, run_id) must raise IntegrityError."""
    run_id = _uid()
    eid = "sha256-duplicate"
    await tmp_db.execute(
        "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
        (run_id, "pending", "init"),
    )
    await tmp_db.execute(
        "INSERT INTO evidence (evidence_id, run_id, agent_type, predicate, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (eid, run_id, "reviewer", "lint_clean", "ok"),
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await tmp_db.execute(
            "INSERT INTO evidence (evidence_id, run_id, agent_type, predicate, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (eid, run_id, "reviewer", "lint_clean", "duplicate"),
        )


# ── attempt + dispatch lifecycle ─────────────────────────────────────────────


async def test_attempt_dispatch_lifecycle(tmp_db: Database) -> None:
    run_id = _uid()
    plan_id = _uid()
    attempt_id = _uid()
    task_id = _uid()
    dispatch_id = _uid()

    # Create run
    await tmp_db.execute(
        "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
        (run_id, "running", "coding"),
    )
    # Create plan
    await tmp_db.execute(
        "INSERT INTO plans (id, run_id, plan_index, approach) VALUES (?, ?, ?, ?)",
        (plan_id, run_id, 0, "incremental"),
    )
    # Create task
    await tmp_db.execute(
        "INSERT INTO tasks (id, run_id, agent_type, phase) VALUES (?, ?, ?, ?)",
        (task_id, run_id, "programmer", "coding"),
    )
    # Create attempt
    await tmp_db.execute(
        "INSERT INTO attempts (id, run_id, plan_id, attempt_index, status) VALUES (?, ?, ?, ?, ?)",
        (attempt_id, run_id, plan_id, 0, "running"),
    )
    # Create dispatch
    await tmp_db.execute(
        "INSERT INTO dispatches (id, attempt_id, task_id, agent_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (dispatch_id, attempt_id, task_id, "programmer", "pending"),
    )

    # Update dispatch to done
    await tmp_db.execute(
        "UPDATE dispatches SET status = ?, output_payload = ?, tokens_used = ? WHERE id = ?",
        ("done", '{"code": "print(1)"}', 1500, dispatch_id),
    )

    row = await tmp_db.fetch_one("SELECT * FROM dispatches WHERE id = ?", (dispatch_id,))
    assert row is not None
    assert row["status"] == "done"
    assert row["tokens_used"] == 1500

    # Update attempt to passed
    await tmp_db.execute(
        "UPDATE attempts SET status = ?, tokens_used = ?, duration_ms = ? WHERE id = ?",
        ("passed", 3000, 12345, attempt_id),
    )

    row = await tmp_db.fetch_one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
    assert row is not None
    assert row["status"] == "passed"
    assert row["duration_ms"] == 12345


async def test_attempt_status_escalated(tmp_db: Database) -> None:
    """Attempts can be set to 'escalated' status."""
    run_id = _uid()
    plan_id = _uid()
    attempt_id = _uid()

    await tmp_db.execute(
        "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
        (run_id, "running", "coding"),
    )
    await tmp_db.execute(
        "INSERT INTO plans (id, run_id, plan_index, approach) VALUES (?, ?, ?, ?)",
        (plan_id, run_id, 0, "fix"),
    )
    await tmp_db.execute(
        "INSERT INTO attempts (id, run_id, plan_id, attempt_index, status, failure_reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (attempt_id, run_id, plan_id, 2, "escalated", "max retries hit"),
    )

    row = await tmp_db.fetch_one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
    assert row is not None
    assert row["status"] == "escalated"
    assert row["failure_reason"] == "max retries hit"


# ── plan creation ────────────────────────────────────────────────────────────


async def test_plan_creation(tmp_db: Database) -> None:
    run_id = _uid()
    p1, p2 = _uid(), _uid()

    await tmp_db.execute(
        "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
        (run_id, "running", "planning"),
    )
    await tmp_db.execute(
        "INSERT INTO plans (id, run_id, plan_index, approach, risk_level, impacted_files, test_scenarios) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (p1, run_id, 0, "add feature X", "medium", '["src/mat/foo.py"]', '["test happy path"]'),
    )
    await tmp_db.execute(
        "INSERT INTO plans (id, run_id, plan_index, approach, risk_level) VALUES (?, ?, ?, ?, ?)",
        (p2, run_id, 1, "refactor Y", "low"),
    )

    rows = await tmp_db.fetch_all(
        "SELECT * FROM plans WHERE run_id = ? ORDER BY plan_index", (run_id,)
    )
    assert len(rows) == 2
    assert rows[0]["approach"] == "add feature X"
    assert rows[0]["risk_level"] == "medium"
    assert rows[1]["plan_index"] == 1


# ── transaction rollback ────────────────────────────────────────────────────


async def test_transaction_rollback_on_error(tmp_db: Database) -> None:
    """Transaction must roll back when an exception occurs."""
    run_id = _uid()
    await tmp_db.execute(
        "INSERT INTO runs (id, status, current_phase) VALUES (?, ?, ?)",
        (run_id, "pending", "init"),
    )

    with pytest.raises(RuntimeError, match="boom"):
        async with tmp_db.transaction() as conn:
            await conn.execute(
                "UPDATE runs SET status = ? WHERE id = ?",
                ("running", run_id),
            )
            raise RuntimeError("boom")

    # Status should still be 'pending' after rollback
    row = await tmp_db.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))
    assert row is not None
    assert row["status"] == "pending"
