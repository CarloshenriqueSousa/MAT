"""Repository for managing tasks, dependencies, and plans."""

from __future__ import annotations

import json

import aiosqlite

from mat.db.connection import Database
from mat.models.contracts import Plan, TestScenario


class TasksRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_plan(self, plan: Plan) -> None:
        query = """
        INSERT INTO plans (id, run_id, plan_index, approach, risk_level, impacted_files, test_scenarios)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """
        await self.db.execute(
            query,
            (
                plan.id,
                plan.run_id,
                plan.plan_index,
                plan.approach,
                plan.risk_level,
                json.dumps(plan.impacted_files),
                json.dumps([s.model_dump() for s in plan.test_scenarios]),
            ),
        )

    async def get_latest_plan(self, run_id: str) -> Plan | None:
        query = "SELECT * FROM plans WHERE run_id = ? ORDER BY plan_index DESC LIMIT 1;"
        row = await self.db.fetch_one(query, (run_id,))
        if not row:
            return None
        scenarios_data = json.loads(row["test_scenarios"])
        return Plan(
            id=row["id"],
            run_id=row["run_id"],
            plan_index=row["plan_index"],
            approach=row["approach"],
            risk_level=row["risk_level"],
            impacted_files=json.loads(row["impacted_files"]),
            test_scenarios=[TestScenario(**s) for s in scenarios_data],
        )

    async def create_task(
        self, task_id: str, run_id: str, agent_type: str, phase: str, spec: str = ""
    ) -> None:
        query = """
        INSERT INTO tasks (id, run_id, agent_type, phase, spec, status)
        VALUES (?, ?, ?, ?, ?, 'pending');
        """
        await self.db.execute(query, (task_id, run_id, agent_type, phase, spec))

    async def add_dependency(self, task_id: str, depends_on_id: str) -> None:
        query = "INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?, ?);"
        await self.db.execute(query, (task_id, depends_on_id))

    async def update_task_status(self, task_id: str, status: str) -> None:
        query = "UPDATE tasks SET status = ? WHERE id = ?;"
        await self.db.execute(query, (status, task_id))

    async def get_task(self, task_id: str) -> aiosqlite.Row | None:
        query = "SELECT * FROM tasks WHERE id = ?;"
        return await self.db.fetch_one(query, (task_id,))
