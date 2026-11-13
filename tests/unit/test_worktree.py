"""Regression tests for Git worktree creation and diff collection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mat.sandbox.worktree import WorktreeManager


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_repo(repo_path: Path) -> tuple[Path, str]:
    repo_path.mkdir()
    _git(repo_path, "init", "--quiet")
    _git(repo_path, "config", "user.name", "MAT tests")
    _git(repo_path, "config", "user.email", "mat-tests@localhost")
    (repo_path / "modified.txt").write_text("original\n", encoding="utf-8")
    (repo_path / "staged.txt").write_text("original staged\n", encoding="utf-8")
    (repo_path / "deleted.txt").write_text("remove me\n", encoding="utf-8")
    _git(repo_path, "add", "--all")
    _git(repo_path, "commit", "--quiet", "-m", "initial")
    return repo_path, _git(repo_path, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_diff_includes_staged_unstaged_new_and_deleted_without_staging(tmp_path):
    repo_path, base_sha = _create_repo(tmp_path / "repo")
    manager = WorktreeManager(tmp_path / "worktrees")
    worktree_path = await manager.create(repo_path, "diff-case", base_sha)

    (worktree_path / "modified.txt").write_text("modified unstaged\n", encoding="utf-8")
    (worktree_path / "staged.txt").write_text("modified staged\n", encoding="utf-8")
    (worktree_path / "deleted.txt").unlink()
    (worktree_path / "new.txt").write_text("new untracked file\n", encoding="utf-8")
    _git(worktree_path, "add", "staged.txt")

    staged_before = _git(worktree_path, "diff", "--cached", "--name-only")
    head_before = _git(worktree_path, "rev-parse", "HEAD")
    diff = await manager.get_diff(worktree_path, base_sha)
    staged_after = _git(worktree_path, "diff", "--cached", "--name-only")

    for path in ("modified.txt", "staged.txt", "deleted.txt", "new.txt"):
        assert path in diff
    assert "new untracked file" in diff
    assert staged_after == staged_before == "staged.txt"
    assert head_before == base_sha
    assert (worktree_path / "new.txt").exists()


@pytest.mark.asyncio
async def test_worktree_starts_at_selected_base_revision(tmp_path):
    repo_path, first_sha = _create_repo(tmp_path / "repo")
    (repo_path / "later.txt").write_text("later commit\n", encoding="utf-8")
    _git(repo_path, "add", "later.txt")
    _git(repo_path, "commit", "--quiet", "-m", "later")

    manager = WorktreeManager(tmp_path / "worktrees")
    worktree_path = await manager.create(repo_path, "selected-base", first_sha)

    assert _git(worktree_path, "rev-parse", "HEAD") == first_sha
    assert not (worktree_path / "later.txt").exists()


@pytest.mark.asyncio
async def test_worktree_operations_ignore_git_environment_overrides(tmp_path, monkeypatch):
    repo_path, base_sha = _create_repo(tmp_path / "repo")
    manager = WorktreeManager(tmp_path / "worktrees")
    worktree_path = await manager.create(repo_path, "environment-case", base_sha)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-git-dir"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-work-tree"))

    await manager.remove(worktree_path)

    assert not worktree_path.exists()


@pytest.mark.asyncio
async def test_worktree_creation_does_not_remove_existing_path(tmp_path):
    repo_path, base_sha = _create_repo(tmp_path / "repo")
    worktree_root = tmp_path / "worktrees"
    existing_path = worktree_root / "collision"
    existing_path.mkdir(parents=True)
    marker = existing_path / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")

    manager = WorktreeManager(worktree_root)
    with pytest.raises(FileExistsError):
        await manager.create(repo_path, "collision", base_sha)
    assert marker.read_text(encoding="utf-8") == "preserve"


@pytest.mark.asyncio
async def test_worktree_rejects_path_traversal_run_id(tmp_path):
    manager = WorktreeManager(tmp_path / "worktrees")
    with pytest.raises(ValueError, match="run_id"):
        await manager.create(tmp_path / "missing-repo", "../outside")
    assert not (tmp_path / "outside").exists()


async def test_worktree_git_context_rejects_symlinked_git_marker(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    external_git = tmp_path / "external-git"
    external_git.mkdir()
    try:
        (repo_path / ".git").symlink_to(external_git, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")

    manager = WorktreeManager(tmp_path / "worktrees")
    with pytest.raises(RuntimeError, match="symlink"):
        await manager.resolve_revision(repo_path, "HEAD")


@pytest.mark.asyncio
async def test_diff_rejects_untracked_symlinks(tmp_path):
    repo_path, base_sha = _create_repo(tmp_path / "repo")
    manager = WorktreeManager(tmp_path / "worktrees")
    worktree_path = await manager.create(repo_path, "symlink-case", base_sha)
    try:
        (worktree_path / "untracked-link").symlink_to(worktree_path / "modified.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")

    with pytest.raises(RuntimeError, match="symlinks novos"):
        await manager.get_diff(worktree_path, base_sha)


@pytest.mark.asyncio
async def test_diff_requires_a_full_commit_sha(tmp_path):
    repo_path, _base_sha = _create_repo(tmp_path / "repo")
    manager = WorktreeManager(tmp_path / "worktrees")
    worktree_path = await manager.create(repo_path, "invalid-base", "HEAD")

    with pytest.raises(ValueError, match="SHA Git completo"):
        await manager.get_diff(worktree_path, "HEAD")