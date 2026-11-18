"""Tela de escolha da branch-base (passo 2 do roadmap, D56).

Nada é escolhido por padrão: a pessoa seleciona uma linha (Enter) e confirma num
diálogo que mostra o SHA completo. A confirmação revalida o commit no repositório.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

from mat.project import (
    BaseMovedError,
    BaseSelection,
    BranchKind,
    BranchRef,
    ProjectError,
    confirm_base,
    list_branches,
)

Lister = Callable[[Path], Awaitable[list[BranchRef]]]
Confirmer = Callable[[Path, BranchRef], Awaitable[BaseSelection]]


def describe(branch: BranchRef) -> str:
    """Markup Rich com nome e título escapados (vêm do repositório-alvo)."""
    current = " (checkout atual)" if branch.is_current else ""
    return (
        f"[b]{escape(branch.name)}[/b]{current} — {branch.kind.value}\n"
        f"Commit: {branch.sha}\n"
        f"Data:   {branch.committed_at}\n"
        f"Título: {escape(branch.subject)}"
    )


class ConfirmBaseScreen(ModalScreen[bool]):
    """Pede confirmação explícita mostrando a revisão exata."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Voltar")]

    def __init__(self, branch: BranchRef) -> None:
        super().__init__()
        self.branch = branch

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Usar esta branch como base?", id="question")
            yield Static(describe(self.branch), id="revision", markup=True)
            yield Static(
                "O trabalho partirá deste commit exato, mesmo que a branch avance depois.",
                id="note",
            )
            with Horizontal(id="buttons"):
                yield Button("Confirmar", variant="success", id="confirm")
                yield Button("Voltar", variant="default", id="cancel")

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class BaseBranchApp(App[BaseSelection | None]):
    """Lista branches locais/remotas e devolve a base confirmada (ou None)."""

    TITLE = "MAT — branch-base"
    CSS = """
    #repo { padding: 0 1; color: $text-muted; }
    #details { height: auto; padding: 1; border: round $primary; }
    ConfirmBaseScreen { align: center middle; }
    #dialog { width: 90; height: auto; padding: 1 2; border: thick $warning; background: $surface; }
    #question { text-style: bold; margin-bottom: 1; }
    #note { color: $text-muted; margin-top: 1; }
    #buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        ("r", "reload", "Recarregar"),
        ("q", "quit_without_choice", "Sair sem escolher"),
    ]

    def __init__(
        self,
        repo_path: Path,
        branches: list[BranchRef],
        *,
        lister: Lister = list_branches,
        confirmer: Confirmer = confirm_base,
    ) -> None:
        super().__init__()
        self.repo_path = repo_path
        self.branches = branches
        self._lister = lister
        self._confirmer = confirmer

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            f"Projeto: {self.repo_path}  •  remotas refletem o último fetch "
            "(Enter escolhe, r recarrega, q sai)",
            id="repo",
        )
        yield DataTable(id="branches", cursor_type="row", zebra_stripes=True)
        yield Static("", id="details", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("", "Tipo", "Branch", "Commit", "Data", "Título")
        self._fill()
        table.focus()

    def _fill(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for index, branch in enumerate(self.branches):
            table.add_row(
                "*" if branch.is_current else "",
                "local" if branch.kind == BranchKind.LOCAL else "remota",
                Text(branch.name),
                branch.sha[:12],
                branch.committed_at[:19].replace("T", " "),
                Text(branch.subject),
                key=str(index),
            )
        self._show_details(0 if self.branches else None)

    def _show_details(self, index: int | None) -> None:
        details = self.query_one("#details", Static)
        if index is None:
            details.update("Nenhuma branch encontrada neste repositório.")
        else:
            details.update(describe(self.branches[index]))

    @on(DataTable.RowHighlighted)
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None:
            self._show_details(int(event.row_key.value))

    @on(DataTable.RowSelected)
    def _selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is None:
            return
        branch = self.branches[int(event.row_key.value)]
        self.push_screen(ConfirmBaseScreen(branch), self._make_confirm_handler(branch))

    def _make_confirm_handler(self, branch: BranchRef):
        async def handle(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                selection = await self._confirmer(self.repo_path, branch)
            except BaseMovedError as exc:
                self.notify(str(exc), severity="warning", timeout=8)
                await self.action_reload()
                return
            except ProjectError as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            self.exit(selection)

        return handle

    async def action_reload(self) -> None:
        try:
            self.branches = await self._lister(self.repo_path)
        except ProjectError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self._fill()

    def action_quit_without_choice(self) -> None:
        self.exit(None)
