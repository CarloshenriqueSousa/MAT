"""Repository for managing attempts and dispatches."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from mat.db.connection import Database


class AttemptsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_attempt(
        self,
        attempt_id: str,
        run_id: str,
        plan_id: str,
        attempt_index: int,
        token_budget: int = 80000,
    ) -> None:
        query = """
        INSERT INTO attempts (id, run_id, plan_id, attempt_index, status, token_budget)
        VALUES (?, ?, ?, ?, 'running', ?);
        """
        await self.db.execute(query, (attempt_id, run_id, plan_id, attempt_index, token_budget))

    async def update_attempt_status(
        self,
        attempt_id: str,
        status: str,
        failure_reason: str | None = None,
        tokens_used: int = 0,
        duration_ms: int = 0,
    ) -> None:
        query = """
        UPDATE attempts
        SET status = ?, failure_reason = ?, tokens_used = tokens_used + ?, duration_ms = duration_ms + ?
        WHERE id = ?;
        """
        await self.db.execute(query, (status, failure_reason, tokens_used, duration_ms, attempt_id))

    async def get_latest_attempt(self, run_id: str) -> aiosqlite.Row | None:
        query = "SELECT * FROM attempts WHERE run_id = ? ORDER BY created_at DESC LIMIT 1;"
        return await self.db.fetch_one(query, (run_id,))

    async def list_attempts_for_run(self, run_id: str) -> list[aiosqlite.Row]:
        query = "SELECT * FROM attempts WHERE run_id = ? ORDER BY created_at ASC;"
        return await self.db.fetch_all(query, (run_id,))

    async def create_dispatch(
        self,
        dispatch_id: str,
        attempt_id: str,
        task_id: str,
        agent_type: str,
        input_payload: dict[str, Any] | None = None,
    ) -> None:
        query = """
        INSERT INTO dispatches (id, attempt_id, task_id, agent_type, status, input_payload)
        VALUES (?, ?, ?, ?, 'running', ?);
        """
        await self.db.execute(
            query,
            (dispatch_id, attempt_id, task_id, agent_type, json.dumps(input_payload or {})),
        )

    async def complete_dispatch(
        self,
        dispatch_id: str,
        status: str,
        output_payload: dict[str, Any] | None = None,
        tokens_used: int = 0,
    ) -> None:
        query = """
        UPDATE dispatches
        SET status = ?, output_payload = ?, tokens_used = ?
        WHERE id = ?;
        """
        await self.db.execute(
            query,
            (status, json.dumps(output_payload or {}), tokens_used, dispatch_id),
        )
