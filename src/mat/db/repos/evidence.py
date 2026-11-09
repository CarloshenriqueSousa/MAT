"""Repository for immutable, content-addressed evidence ledger."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import aiosqlite

from mat.db.connection import Database


def compute_evidence_id(predicate: str, content: str) -> str:
    """Computes content-addressed evidence ID: ev_<sha256>."""
    digest = hashlib.sha256(f"{predicate}:{content}".encode()).hexdigest()
    return f"ev_{digest[:16]}"


class EvidenceRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def record_evidence(
        self,
        run_id: str,
        agent_type: str,
        predicate: str,
        content: Any,
        task_id: str | None = None,
        attempt_id: str | None = None,
        confidence: float = 1.0,
    ) -> str:
        serialized = (
            json.dumps(content, sort_keys=True) if not isinstance(content, str) else content
        )
        ev_id = compute_evidence_id(predicate, serialized)

        query = """
        INSERT INTO evidence (evidence_id, run_id, task_id, attempt_id, agent_type, predicate, content, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(evidence_id, run_id) DO UPDATE SET
            confidence = excluded.confidence;
        """
        await self.db.execute(
            query,
            (ev_id, run_id, task_id, attempt_id, agent_type, predicate, serialized, confidence),
        )
        return ev_id

    async def list_for_run(self, run_id: str) -> list[aiosqlite.Row]:
        query = "SELECT * FROM evidence WHERE run_id = ? ORDER BY created_at ASC;"
        return await self.db.fetch_all(query, (run_id,))
