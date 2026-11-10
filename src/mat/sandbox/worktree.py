"""Git Worktree manager for isolated agent workspaces."""

from __future__ import annotations

import asyncio
import difflib
import os
import subprocess
from pathlib import Path


def _git_env() -> dict[str, str]:
    """Keep host Git from loading user hooks/configuration for target operations."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_AUTHOR_NAME": "MAT",
            "GIT_AUTHOR_EMAIL": "mat@localhost",
            "GIT_COMMITTER_NAME": "MAT",
            "GIT_COMMITTER_EMAIL": "mat@localhost",
        }
    )
    return env


def _git(*args: str, repository_path: Path | None = None) -> list[str]:
    # -c takes precedence over repo-local config, including hooks and filters.
    options = [
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "commit.gpgSign=false",
    ]
    if repository_path is not None:
        config = subprocess.run(
            ["git", "-C", str(repository_path), "config", "--local", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
            capture_output=True,
            text=True,
            check=False,
            env=_git_env(),
        )
        for key in config.stdout.splitlines():
            options.extend(("-c", f"{key}="))
        options.extend(("-c", f"core.attributesFile={os.devnull}"))
    return ["git", *options, *args]


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

        cmd = _git(
            "-C",
            str(repo_path),
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path.resolve()),
            repository_path=repo_path,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Falha ao criar worktree: {stderr.decode('utf-8')}")

        return worktree_path

    async def remove(self, worktree_path: Path) -> None:
        """Remove o worktree com segurança."""
        if not worktree_path.exists():
            return

        cmd = _git("worktree", "remove", "--force", str(worktree_path.resolve()))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        await proc.communicate()

    async def get_diff(self, worktree_path: Path) -> str:
        """Obtém o diff de todas as modificações no worktree."""
        cmd = _git(
            "-C",
            str(worktree_path),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            repository_path=worktree_path,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        stdout, _ = await proc.communicate()
        tracked_diff = stdout.decode("utf-8", errors="replace")

        # `git diff HEAD` omits untracked files. Include regular files only;
        # never follow a target-created symlink while reading from the host.
        status = await asyncio.create_subprocess_exec(
            *_git(
                "-C",
                str(worktree_path),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                repository_path=worktree_path,
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        paths_raw, _ = await status.communicate()
        sections = [tracked_diff] if tracked_diff else []
        for raw_path in paths_raw.split(b"\0"):
            if not raw_path:
                continue
            rel_path = Path(os.fsdecode(raw_path))
            file_path = worktree_path / rel_path
            if file_path.is_symlink() or not file_path.is_file():
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            lines = content.splitlines(keepends=True)
            sections.append(
                "".join(
                    difflib.unified_diff(
                        [], lines, fromfile="/dev/null", tofile=f"b/{rel_path.as_posix()}"
                    )
                )
            )
        return "\n".join(section for section in sections if section)

    async def get_untracked_symlinks(self, worktree_path: Path) -> list[str]:
        """Return new symlinks that cannot be safely included in a host-read diff."""
        proc = await asyncio.create_subprocess_exec(
            *_git(
                "-C",
                str(worktree_path),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                repository_path=worktree_path,
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        output, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError("Não foi possível listar arquivos novos do worktree.")
        paths = [Path(os.fsdecode(item)) for item in output.split(b"\0") if item]
        return [str(path) for path in paths if (worktree_path / path).is_symlink()]

    async def commit_all(self, worktree_path: Path, message: str) -> str:
        """Adiciona e commita todas as mudanças."""
        add_proc = await asyncio.create_subprocess_exec(
            *_git("-C", str(worktree_path), "add", "-A", repository_path=worktree_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        _add_out, add_err = await add_proc.communicate()
        if add_proc.returncode != 0:
            raise RuntimeError(f"git add falhou: {add_err.decode('utf-8', errors='replace')}")
        proc = await asyncio.create_subprocess_exec(
            *_git(
            "-C",
            str(worktree_path),
            "commit",
            "-m",
            message,
                repository_path=worktree_path,
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        _commit_out, commit_err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git commit falhou: {commit_err.decode('utf-8', errors='replace')}")

        rev_proc = await asyncio.create_subprocess_exec(
            *_git(
            "-C",
            str(worktree_path),
            "rev-parse",
            "HEAD",
                repository_path=worktree_path,
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        stdout, _ = await rev_proc.communicate()
        if rev_proc.returncode != 0:
            raise RuntimeError("Não foi possível obter o SHA do commit.")
        return stdout.decode("utf-8").strip()
