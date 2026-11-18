"""MAT Command Line Interface."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from mat.db.connection import Database
from mat.engine.orchestrator import PipelineOrchestrator, ResumeError
from mat.engine.state_machine import RunStatus
from mat.factory import build_simulation_agents
from mat.settings import load_config

console = Console()


async def cmd_run(task: str, supervised: bool = False, db_path: str | None = None) -> int:
    config = load_config()
    db_file = Path(db_path) if db_path else Path(config.mat.paths.db)
    db = Database(db_file)
    await db.init_schema()

    console.print(
        Panel.fit(
            f"[bold cyan]MAT Engine — SIMULAÇÃO COM STUBS[/bold cyan]\n"
            f"[yellow]Não altera o repositório nem executa os gates reais.[/yellow]\n"
            f"Tarefa informativa: [white]{task}[/white]\n"
            f"[dim]Modo:[/dim] {'[yellow]Supervisionado[/yellow]' if supervised else '[green]Autônomo[/green]'}",
            title="Multi-Agent Tenant",
            border_style="cyan",
        )
    )

    try:
        # A CLI ainda só executa simulações; o modo real exige worktree por solicitação.
        agents = build_simulation_agents()
        orchestrator = PipelineOrchestrator(
            config=config,
            db=db,
            supervised=supervised,
            **agents.orchestrator_kwargs(),
        )

        status = await orchestrator.run(task_description=task)

        if status == RunStatus.PASSED:
            console.print(
                "\n[bold yellow]Simulação concluída.[/bold yellow] "
                "Nenhum código foi validado ou aprovado."
            )
            return 0
        elif status == RunStatus.AWAITING_HUMAN:
            console.print(
                "\n[bold yellow]⏸Pipeline pausado em checkpoint humano (`awaiting_human`).[/bold yellow]\n"
                "Revise o plano e use `mat resume <run_id>` para aprovar "
                "ou `mat resume <run_id> --reject` para cancelar."
            )
            return 0
        else:
            console.print(
                "\n[bold red]Pipeline FALHOU. Limites de retry/replan esgotados.[/bold red]"
            )
            return 1
    finally:
        await db.close()


async def cmd_resume(
    run_id: str, *, reject: bool = False, reason: str = "", db_path: str | None = None
) -> int:
    config = load_config()
    db_file = Path(db_path) if db_path else Path(config.mat.paths.db)
    db = Database(db_file)
    await db.init_schema()

    try:
        # Mesmo conjunto da `mat run`: só simulação até a CLI criar worktrees (D52/D53).
        agents = build_simulation_agents()
        orchestrator = PipelineOrchestrator(config=config, db=db, **agents.orchestrator_kwargs())
        try:
            status = await orchestrator.resume(run_id, approve=not reject, reason=reason)
        except ResumeError as exc:
            console.print(f"[bold red]Não foi possível retomar:[/bold red] {exc}")
            return 1

        if status == RunStatus.CANCELLED:
            console.print(f"[yellow]Plano rejeitado; run {run_id} cancelada.[/yellow]")
            return 0
        if status == RunStatus.PASSED:
            console.print(
                "\n[bold yellow]Simulação concluída.[/bold yellow] "
                "Nenhum código foi validado ou aprovado."
            )
            return 0
        if status == RunStatus.AWAITING_HUMAN:
            console.print(
                "\n[bold yellow]⏸Pipeline pausado novamente (`awaiting_human`).[/bold yellow] "
                f"Consulte `mat report {run_id}`."
            )
            return 0
        console.print("\n[bold red]Pipeline FALHOU. Limites de retry/replan esgotados.[/bold red]")
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
        table.add_row("Modo de Execução", str(run["execution_mode"]).upper())
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

    # mat resume <run_id>
    resume_parser = subparsers.add_parser(
        "resume", help="Aprova (ou rejeita) o plano de uma run pausada e a retoma"
    )
    resume_parser.add_argument("run_id", type=str, help="ID da run pausada")
    resume_parser.add_argument(
        "--reject", action="store_true", help="Rejeita o plano e cancela a run"
    )
    resume_parser.add_argument(
        "--reason", type=str, default="", help="Motivo da decisão (fica registrado)"
    )
    resume_parser.add_argument("--db", type=str, default=None, help="Caminho do banco SQLite")

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
    elif args.command == "resume":
        exit_code = asyncio.run(
            cmd_resume(args.run_id, reject=args.reject, reason=args.reason, db_path=args.db)
        )
        sys.exit(exit_code)
    elif args.command == "report":
        exit_code = asyncio.run(cmd_report(args.run_id, args.db))
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
