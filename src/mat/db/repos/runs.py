"""Repository for managing runs and baselines."""

from __future__ import annotations

import aiosqlite

from mat.db.connection import Database
from mat.models.contracts import Baseline


class RunsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        run_id: str,
        status: str = "pending",
        phase: str = "init",
        execution_mode: str = "unknown",
    ) -> aiosqlite.Row:
        query = """
        INSERT INTO runs (id, status, current_phase, execution_mode, plan_count, total_attempts)
        VALUES (?, ?, ?, ?, 0, 0)
        RETURNING *;
        """
        async with self.db.write() as conn:
            cursor = await conn.execute(query, (run_id, status, phase, execution_mode))
            row = await cursor.fetchone()
            return row

    async def get(self, run_id: str) -> aiosqlite.Row | None:
        query = "SELECT * FROM runs WHERE id = ?;"
        return await self.db.fetch_one(query, (run_id,))

    async def update_status(self, run_id: str, status: str, phase: str | None = None) -> None:
        if phase:
            query = """
            UPDATE runs
            SET status = ?, current_phase = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?;
            """
            await self.db.execute(query, (status, phase, run_id))
        else:
            query = """
            UPDATE runs
            SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?;
            """
            await self.db.execute(query, (status, run_id))

    async def increment_attempts(self, run_id: str) -> int:
        query = """
        UPDATE runs
        SET total_attempts = total_attempts + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        RETURNING total_attempts;
        """
        async with self.db.write() as conn:
            cursor = await conn.execute(query, (run_id,))
            row = await cursor.fetchone()
            return row["total_attempts"] if row else 0

    async def increment_plans(self, run_id: str) -> int:
        query = """
        UPDATE runs
        SET plan_count = plan_count + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        RETURNING plan_count;
        """
        async with self.db.write() as conn:
            cursor = await conn.execute(query, (run_id,))
            row = await cursor.fetchone()
            return row["plan_count"] if row else 0

    async def save_baseline(self, baseline: Baseline) -> None:
        query = """
        INSERT INTO baselines (id, run_id, pytest_passed, pytest_total, coverage_pct, ruff_clean, raw_output)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            pytest_passed = excluded.pytest_passed,
            pytest_total = excluded.pytest_total,
            coverage_pct = excluded.coverage_pct,
            ruff_clean = excluded.ruff_clean,
            raw_output = excluded.raw_output;
        """
        await self.db.execute(
            query,
            (
                baseline.id,
                baseline.run_id,
                baseline.pytest_passed,
                baseline.pytest_total,
                baseline.coverage_pct,
                1 if baseline.ruff_clean else 0,
                baseline.raw_output,
            ),
        )

    async def get_baseline(self, run_id: str) -> Baseline | None:
        query = "SELECT * FROM baselines WHERE run_id = ? ORDER BY created_at DESC LIMIT 1;"
        row = await self.db.fetch_one(query, (run_id,))
        if not row:
            return None
        return Baseline(
            id=row["id"],
            run_id=row["run_id"],
            pytest_passed=row["pytest_passed"],
            pytest_total=row["pytest_total"],
            coverage_pct=row["coverage_pct"],
            ruff_clean=bool(row["ruff_clean"]),
            raw_output=row["raw_output"],
        )
