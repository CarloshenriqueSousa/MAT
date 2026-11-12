"""Test the MAT CLI."""

from __future__ import annotations

import pytest

from mat.cli import cmd_report, cmd_run
from mat.db.connection import Database


@pytest.mark.asyncio
async def test_cli_run_and_report(tmp_path):
    db_file = tmp_path / "cli_test.db"

    # 1. Executa task via CLI
    exit_code = await cmd_run("Adicionar endpoint /ping", supervised=False, db_path=str(db_file))
    assert exit_code == 0

    # 2. Descobre run_id criado
    db = Database(db_file)
    run = await db.fetch_one("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1;")
    assert run is not None
    run_id = run["id"]
    run_details = await db.fetch_one(
        "SELECT status, execution_mode FROM runs WHERE id = ?;", (run_id,)
    )
    assert run_details["status"] == "passed"
    assert run_details["execution_mode"] == "simulation"
    await db.close()

    # 3. Executa report via CLI
    report_code = await cmd_report(run_id, db_path=str(db_file))
    assert report_code == 0


@pytest.mark.asyncio
async def test_cli_run_supervised(tmp_path):
    db_file = tmp_path / "cli_supervised.db"
    exit_code = await cmd_run("Refatorar models", supervised=True, db_path=str(db_file))
    assert exit_code == 0

    db = Database(db_file)
    run = await db.fetch_one(
        "SELECT status, current_phase, execution_mode FROM runs ORDER BY created_at DESC LIMIT 1;"
    )
    assert run is not None
    assert run["status"] == "awaiting_human"
    assert run["current_phase"] == "awaiting_human"
    assert run["execution_mode"] == "simulation"
    await db.close()
