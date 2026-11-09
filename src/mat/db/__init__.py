"""Database package — exports the core Database wrapper and raw schema SQL."""

from mat.db.connection import Database
from mat.db.schema import SCHEMA_SQL

__all__ = ["SCHEMA_SQL", "Database"]
