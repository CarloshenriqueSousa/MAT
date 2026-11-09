"""Normalized SQLite schema for the MAT engine.

All tables, indexes, and PRAGMAs are defined here as a single SQL script
executed at database initialisation.
"""

SCHEMA_SQL: str = """
-- ── PRAGMAs ─────────────────────────────────────────────────────────────────
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

-- ── runs ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN (
                       'pending', 'running', 'passed', 'failed',
                       'awaiting_human', 'cancelled'
                   )),
    current_phase  TEXT NOT NULL DEFAULT 'init',
    plan_count     INTEGER NOT NULL DEFAULT 0,
    total_attempts INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ── baselines ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS baselines (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    pytest_passed INTEGER NOT NULL DEFAULT 0,
    pytest_total  INTEGER NOT NULL DEFAULT 0,
    coverage_pct  REAL    NOT NULL DEFAULT 0.0,
    ruff_clean    INTEGER NOT NULL DEFAULT 1,
    raw_output    TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_baselines_run ON baselines(run_id);

-- ── tasks ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    agent_type  TEXT NOT NULL,
    phase       TEXT NOT NULL,
    spec        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_run    ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- ── task_deps  (normalised M2M) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS task_deps (
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);

-- ── plans ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plans (
    id              TEXT PRIMARY KEY,
    run_id          TEXT    NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    plan_index      INTEGER NOT NULL,
    approach        TEXT    NOT NULL DEFAULT '',
    risk_level      TEXT    NOT NULL DEFAULT 'low',
    impacted_files  TEXT    NOT NULL DEFAULT '[]',
    test_scenarios  TEXT    NOT NULL DEFAULT '[]',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_plans_run ON plans(run_id);

-- ── attempts ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS attempts (
    id              TEXT PRIMARY KEY,
    run_id          TEXT    NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    plan_id         TEXT    NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    attempt_index   INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending', 'running', 'passed', 'failed', 'escalated'
                    )),
    failure_reason  TEXT,
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    token_budget    INTEGER NOT NULL DEFAULT 80000,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_attempts_run  ON attempts(run_id);
CREATE INDEX IF NOT EXISTS idx_attempts_plan ON attempts(plan_id);

-- ── dispatches ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dispatches (
    id             TEXT PRIMARY KEY,
    attempt_id     TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    task_id        TEXT NOT NULL REFERENCES tasks(id)    ON DELETE CASCADE,
    agent_type     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'running', 'done', 'failed')),
    input_payload  TEXT NOT NULL DEFAULT '{}',
    output_payload TEXT NOT NULL DEFAULT '{}',
    tokens_used    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_dispatches_attempt ON dispatches(attempt_id);
CREATE INDEX IF NOT EXISTS idx_dispatches_task    ON dispatches(task_id);

-- ── evidence ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id  TEXT NOT NULL,
    run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id      TEXT          REFERENCES tasks(id) ON DELETE SET NULL,
    attempt_id   TEXT          REFERENCES attempts(id) ON DELETE SET NULL,
    agent_type   TEXT NOT NULL,
    predicate    TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    confidence   REAL NOT NULL DEFAULT 1.0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (evidence_id, run_id)
);

-- ── checkpoints ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS checkpoints (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    phase           TEXT NOT NULL,
    state_snapshot  TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id);
""".strip()
