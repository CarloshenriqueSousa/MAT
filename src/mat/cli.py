"""MAT Command Line Interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mat.db.connection import Database
from mat.db.repos import TasksRepo
from mat.engine.orchestrator import PipelineOrchestrator, ResumeError
from mat.engine.state_machine import ExecutionMode, RunStatus
from mat.factory import (
    AgentConfigError,
    AgentSet,
    build_implementation_agents,
    build_planning_agents,
    build_simulation_agents,
    build_unwired_agents,
    check_project_config,
    create_llm_client,
)
from mat.models.contracts import Plan, Workspace
from mat.settings import load_config

console = Console()

if TYPE_CHECKING:
    from mat.agents.llm import LLMClient
    from mat.sandbox.docker_runner import DockerRunner

_BANNERS = {
    ExecutionMode.SIMULATION: (
        "[bold cyan]MAT Engine — SIMULAÇÃO COM STUBS[/bold cyan]\n"
        "[yellow]Os agentes simulados não alteram o código nem executam os gates reais."
        "[/yellow]"
    ),
    ExecutionMode.PLANNING: (
        "[bold cyan]MAT Engine — PLANEJAMENTO[/bold cyan]\n"
        "[yellow]O Analista real lê o worktree e mede o baseline no sandbox. Nenhum "
        "agente edita código; o plano sempre aguarda a sua aprovação.[/yellow]"
    ),
    ExecutionMode.IMPLEMENTATION: (
        "[bold cyan]MAT Engine — IMPLEMENTAÇÃO SUPERVISIONADA[/bold cyan]\n"
        "[yellow]O plano aguarda a sua aprovação; depois o Programador edita só o worktree "
        "(sem commit nem push), o Corretor revisa e o gate roda no sandbox. Tester e "
        "Verificador ainda não estão ligados.[/yellow]"
    ),
}

_PROJECT_BUILDERS = {
    ExecutionMode.PLANNING: build_planning_agents,
    ExecutionMode.IMPLEMENTATION: build_implementation_agents,
}


async def cmd_run(
    task: str,
    supervised: bool = False,
    db_path: str | None = None,
    *,
    repo: str | None = None,
    base: str | None = None,
    base_sha: str | None = None,
    plan_only: bool = False,
    llm: LLMClient | None = None,
    runner: DockerRunner | None = None,
) -> int:
    """Inicia uma run: simulada sem projeto; com ``--repo``, ``implementation`` (D61)
    ou, com ``plan_only``, ``planning`` (D59).

    ``llm`` e ``runner`` permitem injetar dublês nos testes.
    """
    from mat.project import (
        DirtyCheckoutError,
        ProjectError,
        open_project,
        prepare_workspace,
        resolve_selection,
    )
    from mat.sandbox.worktree import WorktreeManager

    project_args = (repo, base, base_sha)
    if any(project_args) and not all(project_args):
        console.print(
            "[bold red]Use --repo, --base e --base-sha juntos[/bold red] "
            "(o `mat open` mostra os valores)."
        )
        return 1

    if plan_only and not all(project_args):
        console.print("[bold red]--plan-only exige --repo, --base e --base-sha.[/bold red]")
        return 1

    config = load_config()
    if not all(project_args):
        mode = ExecutionMode.SIMULATION
    elif plan_only:
        mode = ExecutionMode.PLANNING
    else:
        mode = ExecutionMode.IMPLEMENTATION
    if mode in _PROJECT_BUILDERS:
        # Falha antes de criar branch/worktree se os agentes reais não puderem rodar.
        try:
            check_project_config(config, mode)
            llm = llm or create_llm_client()
        except AgentConfigError as exc:
            console.print(f"[bold red]Configuração incompleta:[/bold red] {escape(str(exc))}")
            return 1
        supervised = True

    db_file = Path(db_path) if db_path else Path(config.mat.paths.db)
    db = Database(db_file)
    await db.init_schema()

    console.print(
        Panel.fit(
            f"{_BANNERS[mode]}\n"
            f"Tarefa: [white]{escape(task)}[/white]\n"
            f"[dim]Modo:[/dim] {'[yellow]Supervisionado[/yellow]' if supervised else '[green]Autônomo[/green]'}",
            title="Multi-Agent Tenant",
            border_style="cyan",
        )
    )

    try:
        run_id = f"run_{uuid.uuid4().hex[:12]}"

        workspace = None
        if repo and base and base_sha:
            try:
                selection = await resolve_selection(open_project(Path(repo)), base, base_sha)
                workspace = await prepare_workspace(
                    WorktreeManager(Path(config.mat.paths.worktrees)), selection, run_id
                )
            except DirtyCheckoutError as exc:
                # Nenhum agente roda; a pausa fica registrada no modo pedido.
                await PipelineOrchestrator(
                    config=config, db=db, **build_unwired_agents(mode).orchestrator_kwargs()
                ).pause_before_start(
                    task,
                    run_id=run_id,
                    predicate="dirty_checkout",
                    reason=str(exc),
                    details={"files": exc.files[:50], "file_count": len(exc.files)},
                    workspace=Workspace(
                        repo_path=selection.repo_path,
                        base_ref=selection.name,
                        base_sha=selection.sha,
                        branch=WorktreeManager.branch_name(run_id),
                    ),
                )
                shown = "\n".join(f"  {escape(f)}" for f in exc.files[:10])
                more = f"\n  ... e mais {len(exc.files) - 10}" if len(exc.files) > 10 else ""
                console.print(
                    f"\n[bold yellow]⏸ Run {run_id} pausada: checkout sujo.[/bold yellow]\n"
                    f"{escape(str(exc))}\n{shown}{more}\n"
                    "Nada foi copiado nem apagado. Faça commit, stash ou descarte as "
                    "alterações e inicie uma nova run."
                )
                return 0
            except ProjectError as exc:
                console.print(
                    f"[bold red]Não foi possível preparar o projeto:[/bold red] {escape(str(exc))}"
                )
                return 1
            console.print(
                f"[green]Worktree criado:[/green] {escape(str(workspace.path))}\n"
                f"Branch [b]{escape(workspace.branch)}[/b] a partir de "
                f"{escape(workspace.base_ref)} @ {workspace.base_sha}"
            )
            assert workspace.path is not None
            try:
                agents = _PROJECT_BUILDERS[mode](
                    config,
                    worktree_path=workspace.path,
                    base_sha=workspace.base_sha,
                    llm=llm,
                    runner=runner,
                )
            except AgentConfigError as exc:
                console.print(
                    f"[bold red]Configuração incompleta:[/bold red] {escape(str(exc))}"
                )
                return 1
        else:
            agents = build_simulation_agents()

        orchestrator = PipelineOrchestrator(
            config=config,
            db=db,
            supervised=supervised,
            **agents.orchestrator_kwargs(),
        )
        status = await orchestrator.run(task_description=task, run_id=run_id, workspace=workspace)

        if status == RunStatus.PASSED:
            console.print(
                f"\n[bold yellow]Simulação concluída ({run_id}).[/bold yellow] "
                "Nenhum código foi validado ou aprovado."
            )
            return 0
        elif status == RunStatus.AWAITING_HUMAN:
            pause = await _latest_pause(db, run_id)
            if pause is not None:
                _print_pause(run_id, *pause)
                return 0
            console.print(
                f"\n[bold yellow]⏸ Run {run_id} pausada em checkpoint humano "
                "(`awaiting_human`).[/bold yellow]\n"
                f"Revise com `mat report {run_id}`; aprove com `mat resume {run_id}` "
                f"ou cancele com `mat resume {run_id} --reject`."
            )
            if mode == ExecutionMode.PLANNING:
                console.print(
                    "[dim]No modo planejamento, aprovar só registra a decisão: a execução "
                    "real ainda não está disponível e nada é alterado no worktree.[/dim]"
                )
            return 0
        else:
            console.print(
                "\n[bold red]Pipeline FALHOU. Limites de retry/replan esgotados.[/bold red] "
                f"Consulte `mat report {run_id}`."
            )
            return 1
    finally:
        await db.close()


def _resume_agents(
    config, run, *, reject: bool, llm: LLMClient | None, runner: DockerRunner | None
) -> AgentSet:
    """Conjunto para retomar ``run``: o mesmo modo com que ela foi criada (D53).

    Rejeitar ou aprovar um plano ``planning`` não chama agentes; aprovar um plano
    ``implementation`` liga os agentes reais sobre o worktree registrado (D61).
    """
    mode = ExecutionMode(run["execution_mode"]) if run is not None else None
    if mode == ExecutionMode.PLANNING or (mode == ExecutionMode.IMPLEMENTATION and reject):
        return build_unwired_agents(mode)
    if mode == ExecutionMode.IMPLEMENTATION:
        if not run["worktree_path"] or not run["base_sha"]:
            raise AgentConfigError("A run não registra worktree e commit-base.")
        return build_implementation_agents(
            config,
            worktree_path=Path(run["worktree_path"]),
            base_sha=run["base_sha"],
            llm=llm,
            runner=runner,
        )
    return build_simulation_agents()


async def cmd_resume(
    run_id: str,
    *,
    reject: bool = False,
    reason: str = "",
    db_path: str | None = None,
    llm: LLMClient | None = None,
    runner: DockerRunner | None = None,
) -> int:
    config = load_config()
    db_file = Path(db_path) if db_path else Path(config.mat.paths.db)
    db = Database(db_file)
    await db.init_schema()

    try:
        run = await db.fetch_one(
            "SELECT execution_mode, worktree_path, base_sha FROM runs WHERE id = ?;", (run_id,)
        )
        planning = run is not None and run["execution_mode"] == ExecutionMode.PLANNING.value
        try:
            agents = _resume_agents(config, run, reject=reject, llm=llm, runner=runner)
        except (AgentConfigError, ValueError) as exc:
            console.print(f"[bold red]Não foi possível retomar:[/bold red] {escape(str(exc))}")
            return 1
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
        if status == RunStatus.AWAITING_HUMAN and planning:
            console.print(
                "\n[bold green]Plano aprovado e registrado.[/bold green] "
                "A execução real (passo 6 do roadmap) ainda não está disponível; "
                "nada foi alterado no worktree. A run permanece em `awaiting_human`."
            )
            return 0
        if status == RunStatus.AWAITING_HUMAN:
            pause = await _latest_pause(db, run_id)
            if pause is not None:
                _print_pause(run_id, *pause)
                return 0
            console.print(
                "\n[bold yellow]⏸Pipeline pausado novamente (`awaiting_human`).[/bold yellow] "
                f"Consulte `mat report {run_id}`."
            )
            return 0
        console.print(
            "\n[bold red]Pipeline FALHOU. Limites de retry/replan esgotados.[/bold red] "
            f"Consulte `mat report {run_id}`."
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
        table.add_row("Modo de Execução", str(run["execution_mode"]).upper())
        table.add_row("Fase Atual", str(run["current_phase"]))
        table.add_row("Planos Gerados", str(run["plan_count"]))
        table.add_row("Tentativas Totais", str(run["total_attempts"]))
        if run["repo_path"]:
            table.add_row("Repositório", Text(run["repo_path"]))
            table.add_row("Base", Text(f"{run['base_ref']} @ {run['base_sha']}"))
            table.add_row("Branch de trabalho", Text(run["work_branch"] or "-"))
            table.add_row("Worktree", Text(run["worktree_path"] or "não criado"))

        if baseline:
            table.add_row(
                "Baseline Inicial",
                f"{baseline['pytest_passed']} testes passados | Cobertura: {baseline['coverage_pct']}%",
            )
        pause = await _latest_pause(db, run_id) if run["status"] == "awaiting_human" else None
        if pause is not None:
            predicate, content = pause
            table.add_row("Pausa", Text(f"{predicate}: {content.get('reason', '')}"))

        console.print(table)
        if pause is not None and pause[0] == "baseline_failing":
            _print_baseline_outputs(pause[1])

        plan = await TasksRepo(db).get_latest_plan(run_id)
        if plan is not None:
            _print_plan(plan)

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


# Pausas que exigem ação humana fora do MAT; `mat resume` ainda não as retoma (passo 9).
_BLOCKING_PAUSES = {
    "baseline_failing": "a base já falha antes de qualquer edição",
    "dirty_checkout": "o checkout original tem alterações",
    "plan_ambiguous": "o Analista considerou o pedido ambíguo",
    "analysis_failed": "o Analista não produziu um plano válido",
    "agent_failure_escalated": "um agente falhou após os retries permitidos",
    "validation_unavailable": (
        "a implementação passou no Corretor e no gate; Tester e Verificador ainda não "
        "estão ligados, então a revisão é sua"
    ),
}


async def _latest_pause(db: Database, run_id: str) -> tuple[str, dict] | None:
    """A pausa bloqueante mais recente da run, se houver (predicado e conteúdo)."""
    placeholders = ", ".join("?" for _ in _BLOCKING_PAUSES)
    row = await db.fetch_one(
        f"SELECT predicate, content FROM evidence WHERE run_id = ? "
        f"AND predicate IN ({placeholders}) ORDER BY created_at DESC, rowid DESC LIMIT 1;",
        (run_id, *_BLOCKING_PAUSES),
    )
    if row is None:
        return None
    try:
        content = json.loads(row["content"])
    except (TypeError, json.JSONDecodeError):
        content = {}
    return row["predicate"], content if isinstance(content, dict) else {}


def _print_pause(run_id: str, predicate: str, content: dict) -> None:
    body = Text()
    body.append(f"Motivo: {_BLOCKING_PAUSES[predicate]}.\n")
    for reason in content.get("reasons") or [content.get("reason", "")]:
        if reason:
            body.append(f"  • {reason}\n")
    if predicate == "baseline_failing":
        body.append(
            "\nNada foi editado e nenhum plano foi gerado. Corrija a base (ou escolha outra) "
            "e inicie uma nova run.\n"
        )
    if predicate == "validation_unavailable":
        files = content.get("modified_files") or []
        body.append("\nArquivos alterados no worktree (sem commit):\n")
        for path in files or ["(nenhum)"]:
            body.append(f"  • {path}\n")
        body.append(
            "Revise o diff no worktree indicado pelo `mat report`; o MAT não faz commit, "
            "push nem PR.\n"
        )
    body.append(f"Detalhes: mat report {run_id}")
    console.print(
        Panel(body, title=Text(f"⏸ Run {run_id} pausada"), border_style="yellow")
    )


def _print_baseline_outputs(content: dict) -> None:
    for key, title in (("pytest_output_tail", "pytest"), ("ruff_output_tail", "ruff")):
        output = content.get(key)
        if output:
            console.print(Panel(Text(output), title=f"Saída do {title} na base", border_style="red"))


def _print_plan(plan: Plan) -> None:
    """Mostra o plano para revisão humana; o conteúdo vem do LLM e nunca vira markup."""
    body = Text()
    body.append("Risco: ", style="bold")
    body.append(f"{plan.risk_level}\n\n")
    body.append("Abordagem\n", style="bold")
    body.append(f"{plan.approach}\n")
    body.append("\nArquivos impactados\n", style="bold")
    for path in plan.impacted_files or ["(nenhum informado)"]:
        body.append(f"  • {path}\n")
    body.append("\nCenários de teste\n", style="bold")
    if not plan.test_scenarios:
        body.append("  (nenhum informado)\n")
    for scenario in plan.test_scenarios:
        body.append(f"  • [{scenario.type}] {scenario.name}")
        body.append(f": {scenario.description}\n" if scenario.description else "\n")
    console.print(
        Panel(body, title=Text(f"Plano {plan.plan_index} ({plan.id})"), border_style="yellow")
    )


async def cmd_open(repo: str) -> int:
    """Abre um projeto na TUI e pede a escolha confirmada da branch-base (D56)."""
    from mat.project import ProjectError, list_branches, open_project

    try:
        from mat.tui.base_branch import BaseBranchApp
    except ImportError:
        console.print(
            "[bold red]A TUI requer o Textual.[/bold red] "
            "Instale com: uv run --python 3.12 --extra tui mat open <repo>"
        )
        return 1

    try:
        repo_path = open_project(Path(repo))
        branches = await list_branches(repo_path)
    except ProjectError as exc:
        console.print(f"[bold red]Não foi possível abrir o projeto:[/bold red] {escape(str(exc))}")
        return 1
    if not branches:
        console.print("[bold red]O repositório não tem branches.[/bold red]")
        return 1

    selection = await BaseBranchApp(repo_path, branches).run_async()
    if selection is None:
        console.print("[yellow]Nenhuma branch-base escolhida.[/yellow]")
        return 1
    console.print(
        Panel.fit(
            f"Projeto: {escape(str(selection.repo_path))}\n"
            f"Branch-base: {escape(selection.name)} ({escape(selection.refname)})\n"
            f"Commit: {selection.sha}",
            title="Branch-base confirmada",
            border_style="green",
        )
    )
    console.print(
        "Para iniciar uma run nesta base:\n"
        f'  mat run "<pedido>" --repo "{escape(str(selection.repo_path))}" '
        f'--base "{escape(selection.name)}" --base-sha {selection.sha}'
    )
    return 0


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
    run_parser.add_argument("--repo", type=str, default=None, help="Raiz do repositório-alvo")
    run_parser.add_argument("--base", type=str, default=None, help="Branch-base (do mat open)")
    run_parser.add_argument(
        "--base-sha", type=str, default=None, help="SHA completo da base (do mat open)"
    )
    run_parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Com --repo: só gera o plano para revisão; nenhum agente edita código",
    )

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

    # mat open <repo>
    open_parser = subparsers.add_parser("open", help="Abre um projeto e escolhe a branch-base")
    open_parser.add_argument("repo", type=str, help="Caminho da raiz do repositório Git")

    # mat report <run_id>
    report_parser = subparsers.add_parser(
        "report", help="Exibe relatório de custo e métricas de uma run"
    )
    report_parser.add_argument("run_id", type=str, help="ID da run")
    report_parser.add_argument("--db", type=str, default=None, help="Caminho do banco SQLite")

    args = parser.parse_args()

    if args.command == "run":
        exit_code = asyncio.run(
            cmd_run(
                args.task,
                args.supervised,
                args.db,
                repo=args.repo,
                base=args.base,
                base_sha=args.base_sha,
                plan_only=args.plan_only,
            )
        )
        sys.exit(exit_code)
    elif args.command == "resume":
        exit_code = asyncio.run(
            cmd_resume(args.run_id, reject=args.reject, reason=args.reason, db_path=args.db)
        )
        sys.exit(exit_code)
    elif args.command == "open":
        sys.exit(asyncio.run(cmd_open(args.repo)))
    elif args.command == "report":
        exit_code = asyncio.run(cmd_report(args.run_id, args.db))
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
