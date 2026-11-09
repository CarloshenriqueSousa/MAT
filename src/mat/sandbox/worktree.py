"""Git Worktree manager for isolated agent workspaces."""

from __future__ import annotations

import asyncio
from pathlib import Path


class WorktreeManager:
    def __init__(self, base_worktree_dir: Path) -> None:
        self.base_dir = base_worktree_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def create(self, repo_path: Path, run_id: str) -> Path:
        """Cria um novo worktree isolado na branch mat/{run_id}."""
        worktree_path = self.base_dir / run_id
        branch_name = f"mat/{run_id}"

        # Se já existir, remove antes
        if worktree_path.exists():
            await self.remove(worktree_path)

        cmd = [
            "git",
            "-C",
            str(repo_path),
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path.resolve()),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Falha ao criar worktree: {stderr.decode('utf-8')}")

        return worktree_path

    async def remove(self, worktree_path: Path) -> None:
        """Remove o worktree com segurança."""
        if not worktree_path.exists():
            return

        cmd = ["git", "worktree", "remove", "--force", str(worktree_path.resolve())]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

    async def get_diff(self, worktree_path: Path) -> str:
        """Obtém o diff de todas as modificações no worktree."""
        cmd = ["git", "-C", str(worktree_path), "diff", "HEAD"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="replace")

    async def commit_all(self, worktree_path: Path, message: str) -> str:
        """Adiciona e commita todas as mudanças."""
        await asyncio.create_subprocess_exec("git", "-C", str(worktree_path), "add", "-A")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(worktree_path),
            "commit",
            "-m",
            message,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        rev_proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(worktree_path),
            "rev-parse",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await rev_proc.communicate()
        return stdout.decode("utf-8").strip()
