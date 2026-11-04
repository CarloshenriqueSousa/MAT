"""SQLite Database Schema for MAT (Write-Ahead Logging).

Inspirado no Blackboard Pattern do Orca e no Evidence Ledger do REA.
Totalmente normalizado, sem JSON para relacionamentos de dependência,
com suporte a baselines, checkpoints, dispatches e chaves compostas
para deduplicação de evidências sem colisão entre runs.
"""

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

-- 1. Runs: Cada execução ponta a ponta do pipeline
CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,
    objective           TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'running', 'completed', 'failed', 'awaiting_human', 'cancelled')),
    current_phase       TEXT NOT NULL DEFAULT 'analysis'
                        CHECK(current_phase IN ('analysis', 'programming', 'testing', 'verification', 'completed', 'failed')),
    current_plan_id     TEXT,
    plan_count          INTEGER NOT NULL DEFAULT 1,
    total_attempts      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    metadata            TEXT DEFAULT '{}'
);

-- 2. Baselines: Medições antes de qualquer código ser alterado (pytest, ruff, coverage)
CREATE TABLE IF NOT EXISTS baselines (
    run_id              TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    pytest_passed       INTEGER NOT NULL DEFAULT 0,
    pytest_total        INTEGER NOT NULL DEFAULT 0,
    coverage_pct        REAL NOT NULL DEFAULT 0.0,
    ruff_clean          INTEGER NOT NULL DEFAULT 1,
    raw_output          TEXT DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 3. Tasks: Unidades de trabalho especializadas
CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    agent_type          TEXT NOT NULL
                        CHECK(agent_type IN ('analyst', 'programmer', 'tester_happy', 'tester_bypass',
                                             'tester_breaker', 'tester_consolidator', 'tester_verifier')),
    phase               TEXT NOT NULL
                        CHECK(phase IN ('analysis', 'programming', 'testing', 'verification')),
    spec                TEXT NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'ready', 'running', 'completed', 'failed', 'blocked', 'skipped')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT
);

-- 4. Task Dependencies (Normalizado: sem JSON para dependências)
CREATE TABLE IF NOT EXISTS task_deps (
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id)
);

-- 5. Attempts: Cada ciclo de tentativa de implementação + teste
CREATE TABLE IF NOT EXISTS attempts (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    plan_index          INTEGER NOT NULL DEFAULT 1,
    attempt_index       INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK(status IN ('running', 'passed', 'failed', 'escalated')),
    failure_reason      TEXT,
    tokens_used         INTEGER NOT NULL DEFAULT 0,
    token_budget        INTEGER NOT NULL DEFAULT 80000,
    duration_ms         INTEGER DEFAULT 0,
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at         TEXT
);

-- 6. Dispatches: Atribuição de uma task específica dentro de uma attempt
CREATE TABLE IF NOT EXISTS dispatches (
    id                  TEXT PRIMARY KEY,
    attempt_id          TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_type          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'dispatched'
                        CHECK(status IN ('dispatched', 'running', 'completed', 'failed')),
    input_payload       TEXT DEFAULT '{}',
    output_payload      TEXT DEFAULT '{}',
    tokens_used         INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at         TEXT
);

-- 7. Evidence: Ledger de observações e resultados (content-addressed, PK composta por run)
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id         TEXT NOT NULL,
    run_id              TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id             TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    attempt_id          TEXT REFERENCES attempts(id) ON DELETE SET NULL,
    agent_type          TEXT NOT NULL,
    predicate           TEXT NOT NULL,
    content             TEXT NOT NULL,
    confidence          TEXT NOT NULL DEFAULT 'observed'
                        CHECK(confidence IN ('observed', 'derived', 'inferred')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (evidence_id, run_id)
);

-- 8. Checkpoints: Captura do estado para suspensão, retomada e Human-in-the-Loop
CREATE TABLE IF NOT EXISTS checkpoints (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    phase               TEXT NOT NULL,
    state_snapshot      TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Índices estratégicos para performance
CREATE INDEX IF NOT EXISTS idx_tasks_run_status ON tasks(run_id, status);
CREATE INDEX IF NOT EXISTS idx_attempts_run ON attempts(run_id, plan_index, attempt_index);
CREATE INDEX IF NOT EXISTS idx_dispatches_attempt ON dispatches(attempt_id, status);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);
"""
