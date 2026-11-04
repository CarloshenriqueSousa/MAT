"""Database connection and lifecycle manager for MAT (aiosqlite with WAL mode)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import aiosqlite

from mat_core.db.schema import SCHEMA_SQL


class Database:
    """Gerenciador central do SQLite Blackboard."""

    def __init__(self, db_path: str | Path = "data/mat.db"):
        self.db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        """Abre conexão com WAL mode e pragmas de resiliência configurados."""
        if self._conn is not None:
            return self._conn

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        # Aplica pragmas de concorrência e integridade
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA synchronous = NORMAL;")
        await self._conn.execute("PRAGMA busy_timeout = 5000;")
        await self._conn.execute("PRAGMA foreign_keys = ON;")

        return self._conn

    async def init_schema(self) -> None:
        """Cria todas as tabelas e índices se não existirem."""
        conn = await self.connect()
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()

    async def close(self) -> None:
        """Fecha a conexão com segurança."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Context manager para operações atômicas."""
        conn = await self.connect()
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    async def execute(self, query: str, parameters: tuple | list | dict = ()) -> aiosqlite.Cursor:
        conn = await self.connect()
        cursor = await conn.execute(query, parameters)
        await conn.commit()
        return cursor

    async def fetch_one(
        self, query: str, parameters: tuple | list | dict = ()
    ) -> aiosqlite.Row | None:
        conn = await self.connect()
        async with conn.execute(query, parameters) as cursor:
            return await cursor.fetchone()

    async def fetch_all(
        self, query: str, parameters: tuple | list | dict = ()
    ) -> list[aiosqlite.Row]:
        conn = await self.connect()
        async with conn.execute(query, parameters) as cursor:
            return await cursor.fetchall()
