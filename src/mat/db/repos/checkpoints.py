"""Repository for saving and restoring execution checkpoints."""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

from mat.db.connection import Database


class CheckpointsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def save_checkpoint(self, run_id: str, phase: str, snapshot: dict[str, Any]) -> str:
        chk_id = f"chk_{uuid.uuid4().hex[:12]}"
        query = """
        INSERT INTO checkpoints (id, run_id, phase, state_snapshot)
        VALUES (?, ?, ?, ?);
        """
        await self.db.execute(query, (chk_id, run_id, phase, json.dumps(snapshot)))
        return chk_id

    async def get_latest_checkpoint(self, run_id: str) -> aiosqlite.Row | None:
        query = "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY created_at DESC LIMIT 1;"
        return await self.db.fetch_one(query, (run_id,))
