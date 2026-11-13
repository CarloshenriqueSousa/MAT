"""Async SQLite database wrapper with WAL-mode reader/writer connections."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

from mat.db.schema import SCHEMA_SQL, SCHEMA_VERSION


class Database:
    """Async SQLite database with separate reader/writer connections.

    WAL mode allows concurrent reads while serialising writes via an
    ``asyncio.Lock``.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._writer: aiosqlite.Connection | None = None
        self._reader: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open reader and writer connections."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._writer = await aiosqlite.connect(str(self._db_path))
        self._writer.row_factory = aiosqlite.Row
        await self._writer.execute("PRAGMA journal_mode = WAL")
        await self._writer.execute("PRAGMA synchronous  = NORMAL")
        await self._writer.execute("PRAGMA busy_timeout = 5000")
        await self._writer.execute("PRAGMA foreign_keys = ON")

        self._reader = await aiosqlite.connect(str(self._db_path))
        self._reader.row_factory = aiosqlite.Row
        await self._reader.execute("PRAGMA journal_mode = WAL")
        await self._reader.execute("PRAGMA busy_timeout = 5000")
        await self._reader.execute("PRAGMA foreign_keys = ON")

    async def init_schema(self) -> None:
        """Execute the full schema DDL on the writer connection."""
        if self._writer is None:
            await self.connect()
        assert self._writer is not None
        version_cursor = await self._writer.execute("PRAGMA user_version;")
        version_row = await version_cursor.fetchone()
        current_version = int(version_row[0]) if version_row else 0
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Banco MAT versão {current_version} não é compatível com esta versão "
                f"(máximo {SCHEMA_VERSION})."
            )

        # PRAGMA statements must be executed individually
        for statement in SCHEMA_SQL.split(";"):
            stmt = statement.strip()
            if stmt:
                await self._writer.execute(stmt)
        cursor = await self._writer.execute("PRAGMA table_info(runs);")
        run_columns = {row["name"] for row in await cursor.fetchall()}
        if "execution_mode" not in run_columns:
            await self._writer.execute(
                "ALTER TABLE runs ADD COLUMN execution_mode TEXT NOT NULL "
                "DEFAULT 'unknown' CHECK (execution_mode IN ('unknown', 'simulation', 'real'));"
            )
        if "base_sha" not in run_columns:
            await self._writer.execute("ALTER TABLE runs ADD COLUMN base_sha TEXT;")
        await self._writer.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
        await self._writer.commit()

    async def close(self) -> None:
        """Close both connections."""
        for conn in (self._reader, self._writer):
            if conn is not None:
                await conn.close()
        self._reader = None
        self._writer = None

    async def _ensure_connected(self) -> None:
        if self._writer is None:
            await self.connect()

    # ── transaction context manager ──────────────────────────────────────

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Acquire the write lock and yield the writer inside a transaction.

        Commits on clean exit; rolls back on any exception including
        ``asyncio.CancelledError``.
        """
        await self._ensure_connected()
        assert self._writer is not None
        async with self._write_lock:
            await self._writer.execute("BEGIN")
            try:
                yield self._writer
                await self._writer.execute("COMMIT")
            except BaseException:
                await self._writer.execute("ROLLBACK")
                raise

    write = transaction

    # ── helpers ──────────────────────────────────────────────────────────

    async def execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> aiosqlite.Cursor:
        """Execute a write query under the write lock and auto-commit."""
        await self._ensure_connected()
        assert self._writer is not None
        async with self._write_lock:
            cursor = await self._writer.execute(sql, params)
            await self._writer.commit()
            return cursor

    async def fetch_one(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> aiosqlite.Row | None:
        """Run a read query and return the first row (or None)."""
        await self._ensure_connected()
        assert self._reader is not None
        cursor = await self._reader.execute(sql, params)
        return await cursor.fetchone()  # type: ignore[return-value]

    async def fetch_all(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[aiosqlite.Row]:
        """Run a read query and return all rows."""
        await self._ensure_connected()
        assert self._reader is not None
        cursor = await self._reader.execute(sql, params)
        return await cursor.fetchall()  # type: ignore[return-value]
