"""Abertura de projeto, listagem de branches e TUI de branch-base (D56)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static

from mat.project import (
    BaseMovedError,
    BranchKind,
    ProjectError,
    confirm_base,
    list_branches,
    open_project,
)
from mat.tui.base_branch import BaseBranchApp, ConfirmBaseScreen


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(message, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path) -> tuple[Path, Path]:
    """`upstream` com main e feature; `clone` com main local e origin/* remotas."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "--quiet", "-b", "main")
    git(upstream, "config", "user.name", "MAT tests")
    git(upstream, "config", "user.email", "mat-tests@localhost")
    commit(upstream, "a.txt", "primeiro")
    git(upstream, "checkout", "--quiet", "-b", "feature")
    commit(upstream, "b.txt", "[bold]título com markup[/bold]")
    git(upstream, "checkout", "--quiet", "main")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(upstream), str(clone)], check=True, capture_output=True
    )
    git(clone, "config", "user.name", "MAT tests")
    git(clone, "config", "user.email", "mat-tests@localhost")
    return upstream, clone


async def test_lists_local_then_remote_without_symbolic_head(repos):
    upstream, clone = repos

    branches = await list_branches(open_project(clone))

    assert [(b.kind, b.name) for b in branches] == [
        (BranchKind.LOCAL, "main"),
        (BranchKind.REMOTE, "origin/feature"),
        (BranchKind.REMOTE, "origin/main"),
    ]
    main, feature = branches[0], branches[1]
    assert main.is_current is True
    assert feature.is_current is False
    assert feature.sha == git(upstream, "rev-parse", "feature")
    assert feature.subject == "[bold]título com markup[/bold]"
    assert feature.refname == "refs/remotes/origin/feature"


async def test_confirm_base_pins_exact_commit(repos):
    _, clone = repos
    repo = open_project(clone)
    feature = (await list_branches(repo))[1]

    selection = await confirm_base(repo, feature)

    assert selection.sha == feature.sha
    assert selection.refname == "refs/remotes/origin/feature"
    assert selection.repo_path == repo


async def test_confirm_base_refuses_branch_that_moved(repos):
    _, clone = repos
    repo = open_project(clone)
    main = (await list_branches(repo))[0]
    commit(clone, "c.txt", "avançou depois da listagem")

    with pytest.raises(BaseMovedError, match="main mudou"):
        await confirm_base(repo, main)


def test_open_project_rejects_non_repository(tmp_path):
    with pytest.raises(ProjectError, match="não é a raiz"):
        open_project(tmp_path)
    with pytest.raises(ProjectError, match="não encontrado"):
        open_project(tmp_path / "inexistente")


async def test_target_repository_hooks_are_not_executed(repos, tmp_path):
    _, clone = repos
    marker = tmp_path / "hook-ran"
    hook = clone / ".git" / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    repo = open_project(clone)

    branches = await list_branches(repo)
    await confirm_base(repo, branches[0])

    assert not marker.exists()


# ── TUI ──────────────────────────────────────────────────────────────────────


async def test_tui_requires_explicit_confirmation(repos):
    _, clone = repos
    repo = open_project(clone)
    branches = await list_branches(repo)
    app = BaseBranchApp(repo, branches)

    async with app.run_test() as pilot:
        await pilot.press("down", "enter")  # escolhe origin/feature
        assert isinstance(app.screen, ConfirmBaseScreen)
        revision = str(app.screen.query_one("#revision", Static).render())
        assert branches[1].sha in revision  # SHA completo, não abreviado
        assert "título com markup" in revision
        await pilot.click("#confirm")
        await pilot.pause()

    assert app.return_value is not None
    assert app.return_value.name == "origin/feature"
    assert app.return_value.sha == branches[1].sha


async def test_tui_back_and_quit_choose_nothing(repos):
    _, clone = repos
    repo = open_project(clone)
    app = BaseBranchApp(repo, await list_branches(repo))

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.click("#cancel")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmBaseScreen)
        await pilot.press("q")
        await pilot.pause()

    assert app.return_value is None


async def test_tui_reloads_when_branch_moved(repos):
    _, clone = repos
    repo = open_project(clone)
    app = BaseBranchApp(repo, await list_branches(repo))

    async with app.run_test() as pilot:
        commit(clone, "c.txt", "avançou")
        await pilot.press("enter")  # main, com SHA antigo
        await pilot.click("#confirm")
        await pilot.pause()
        assert app.is_running  # não aceitou a base desatualizada
        assert app.branches[0].sha == git(clone, "rev-parse", "main")
        table = app.query_one(DataTable)
        assert table.row_count == 3
        await pilot.press("q")


async def test_cli_open_rejects_non_repository(tmp_path):
    from mat.cli import cmd_open

    assert await cmd_open(str(tmp_path)) == 1
