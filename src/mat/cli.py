"""MAT Command Line Interface."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from mat.agents.stubs import (
    AnalystStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.db.connection import Database
from mat.engine.orchestrator import PipelineOrchestrator
from mat.engine.state_machine import RunStatus
from mat.settings import load_config

console = Console()


async def cmd_run(task: str, supervised: bool = False, db_path: str | None = None) -> int:
    config = load_config()
    db_file = Path(db_path) if db_path else Path(config.mat.paths.db)
    db = Database(db_file)
    await db.init_schema()

    console.print(
        Panel.fit(
            f"[bold cyan]MAT Engine[/bold cyan] – Executando Task:\n[white]{task}[/white]\n"
            f"[dim]Modo:[/dim] {'[yellow]Supervisionado[/yellow]' if supervised else '[green]Autônomo[/green]'}",
            title="Multi-Agent Tenant",
            border_style="cyan",
        )
    )

    try:
        orchestrator = PipelineOrchestrator(
            config=config,
            db=db,
            analyst=AnalystStub(),
            programmer=ProgrammerStub(),
            reviewer=ReviewerStub(approve_on_turn=1),
            tester_happy=TesterHappyStub(passed=True),
            verifier=VerifierStub(approved=True),
            supervised=supervised,
        )

        status = await orchestrator.run(task_description=task)

        if status == RunStatus.PASSED:
            console.print("\n[bold green]Task APROVADA pelo Pipeline do MAT![/bold green]")
            return 0
        elif status == RunStatus.AWAITING_HUMAN:
            console.print(
                "\n[bold yellow]⏸Pipeline pausado em checkpoint humano (`awaiting_human`).[/bold yellow]\n"
                "Use [bold cyan]mat resume <run_id>[/bold cyan] para continuar após aprovar o plano."
            )
            return 0
        else:
            console.print(
                "\n[bold red]Pipeline FALHOU. Limites de retry/replan esgotados.[/bold red]"
            )
            return 1
    finally:
        await db.close()


async def cmd_report(run_id: str, db_path: str | None = None) -> int:
    config = load_config()
    db_file = Path(db_path) if db_path else Path(config.mat.paths.db)
    db = Database(db_file)
    await db.init_schema()

    try:
        run = await db.fetch_one("SELECT * FROM runs WHERE id = ?;", (run_id,))
        if not run:
            console.print(f"[bold red]Run '{run_id}' não encontrada no banco.[/bold red]")
            return 1

        attempts = await db.fetch_all(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY created_at ASC;", (run_id,)
        )
        baseline = await db.fetch_one("SELECT * FROM baselines WHERE run_id = ?;", (run_id,))

        table = Table(title=f"📊 Relatório de Execução — Run: {run_id}", border_style="cyan")
        table.add_column("Métrica", style="bold white")
        table.add_column("Valor", style="green")

        table.add_row("Status Final", str(run["status"]).upper())
        table.add_row("Fase Atual", str(run["current_phase"]))
        table.add_row("Planos Gerados", str(run["plan_count"]))
        table.add_row("Tentativas Totais", str(run["total_attempts"]))

        if baseline:
            table.add_row(
                "Baseline Inicial",
                f"{baseline['pytest_passed']} testes passados | Cobertura: {baseline['coverage_pct']}%",
            )

        console.print(table)

        if attempts:
            att_table = Table(title="Detalhe das Tentativas", border_style="magenta")
            att_table.add_column("Attempt ID")
            att_table.add_column("Plano")
            att_table.add_column("Tentativa")
            att_table.add_column("Status")
            att_table.add_column("Tokens Usados")
            att_table.add_column("Motivo Falha")

            for att in attempts:
                att_table.add_row(
                    att["id"],
                    str(att["plan_id"]),
                    str(att["attempt_index"]),
                    att["status"],
                    str(att["tokens_used"]),
                    att["failure_reason"] or "-",
                )
            console.print(att_table)

        return 0
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MAT – Multi-Agent Tenant CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # mat run "..."
    run_parser = subparsers.add_parser("run", help="Executa uma nova task no pipeline")
    run_parser.add_argument("task", type=str, help="Descrição da task / backlog")
    run_parser.add_argument(
        "--supervised", action="store_true", help="Pausa após o plano para aprovação humana"
    )
    run_parser.add_argument("--db", type=str, default=None, help="Caminho do banco SQLite")

    # mat report <run_id>
    report_parser = subparsers.add_parser(
        "report", help="Exibe relatório de custo e métricas de uma run"
    )
    report_parser.add_argument("run_id", type=str, help="ID da run")
    report_parser.add_argument("--db", type=str, default=None, help="Caminho do banco SQLite")

    args = parser.parse_args()

    if args.command == "run":
        exit_code = asyncio.run(cmd_run(args.task, args.supervised, args.db))
        sys.exit(exit_code)
    elif args.command == "report":
        exit_code = asyncio.run(cmd_report(args.run_id, args.db))
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
