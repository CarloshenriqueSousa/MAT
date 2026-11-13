"""Git Worktree manager for isolated agent workspaces."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path


def _git_env() -> dict[str, str]:
    """Keep host Git from loading user hooks/configuration for target operations."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
        }
    )
    return env


def _git_context(worktree_path: Path) -> list[str]:
    marker = worktree_path / ".git"
    if marker.is_symlink():
        raise RuntimeError(f"O marcador .git não pode ser um symlink: {worktree_path}.")
    if marker.is_dir():
        git_dir = marker
    elif marker.is_file():
        marker_content = marker.read_text(encoding="utf-8").strip()
        prefix = "gitdir: "
        if not marker_content.startswith(prefix):
            raise RuntimeError(f"Marcador .git inválido em {worktree_path}.")
        git_dir = Path(marker_content[len(prefix) :])
        if not git_dir.is_absolute():
            git_dir = worktree_path / git_dir
    else:
        raise RuntimeError(f"Não foi possível localizar .git em {worktree_path}.")

    resolved_git_dir = git_dir.resolve(strict=True)
    if not resolved_git_dir.is_dir():
        raise RuntimeError(f"Diretório Git inválido para {worktree_path}.")
    return ["--git-dir", str(resolved_git_dir), "--work-tree", str(worktree_path.resolve())]


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
        git_context = _git_context(repository_path)
        config = subprocess.run(
            [
                "git",
                *git_context,
                "config",
                "--local",
                "--name-only",
                "--get-regexp",
                r"^filter\..*\.(clean|smudge|process)$",
            ],
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

    async def resolve_revision(self, repo_path: Path, revision: str) -> str:
        """Resolve uma revisão do repositório para um SHA de commit imutável."""
        proc = await asyncio.create_subprocess_exec(
            *_git(
                *_git_context(repo_path),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{commit}}",
                repository_path=repo_path,
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        stdout, stderr = await proc.communicate()
        resolved = stdout.decode("ascii", errors="replace").strip()
        if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", resolved):
            raise ValueError(f"Revisão Git inválida '{revision}': {stderr.decode(errors='replace')}")
        return resolved

    async def create(
        self, repo_path: Path, run_id: str, base_revision: str = "HEAD"
    ) -> Path:
        """Cria um worktree na branch mat/{run_id}, baseado na revisão escolhida."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id):
            raise ValueError("run_id contém caracteres não permitidos para worktree/branch.")
        worktree_path = self.base_dir / run_id
        branch_name = f"mat/{run_id}"

        if worktree_path.exists():
            raise FileExistsError(f"O caminho de worktree já existe: {worktree_path}")

        base_sha = await self.resolve_revision(repo_path, base_revision)

        cmd = _git(
            *_git_context(repo_path),
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path.resolve()),
            base_sha,
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

        cmd = _git(
            *_git_context(worktree_path),
            "worktree",
            "remove",
            "--force",
            str(worktree_path.resolve()),
            repository_path=worktree_path,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        await proc.communicate()

    async def get_diff(self, worktree_path: Path, base_sha: str) -> str:
        """Obtém staged, unstaged e novos arquivos comparando com o SHA-base."""
        if not re.fullmatch(r"[0-9a-f]{40,64}", base_sha):
            raise ValueError("base_sha deve ser um SHA Git completo.")

        git_context = _git_context(worktree_path)
        cmd = _git(
            *git_context,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            base_sha,
            "--",
            repository_path=worktree_path,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                "Não foi possível gerar diff contra base_sha: "
                + stderr.decode("utf-8", errors="replace")
            )
        tracked_diff = stdout.decode("utf-8", errors="replace")

        status = await asyncio.create_subprocess_exec(
            *_git(
                *git_context,
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
        if status.returncode != 0:
            raise RuntimeError("Não foi possível listar arquivos novos do worktree.")

        sections = [tracked_diff] if tracked_diff else []
        for raw_path in paths_raw.split(b"\0"):
            if not raw_path:
                continue
            rel_path = Path(os.fsdecode(raw_path))
            file_path = worktree_path / rel_path
            if file_path.is_symlink():
                raise RuntimeError(
                    "O worktree contém symlinks novos, que não são aceitos pelo fluxo seguro: "
                    + rel_path.as_posix()
                )
            if not file_path.is_file():
                continue
            untracked = await asyncio.create_subprocess_exec(
                *_git(
                    *git_context,
                    "diff",
                    "--no-index",
                    "--binary",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--",
                    os.devnull,
                    rel_path.as_posix(),
                    repository_path=worktree_path,
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=worktree_path,
                env=_git_env(),
            )
            untracked_stdout, untracked_stderr = await untracked.communicate()
            if untracked.returncode not in (0, 1):
                raise RuntimeError(
                    "Não foi possível incluir um arquivo novo no diff: "
                    + untracked_stderr.decode("utf-8", errors="replace")
                )
            sections.append(untracked_stdout.decode("utf-8", errors="replace"))
        return "\n".join(section for section in sections if section)

    async def get_untracked_symlinks(self, worktree_path: Path) -> list[str]:
        """Return new symlinks that cannot be safely included in a host-read diff."""
        proc = await asyncio.create_subprocess_exec(
            *_git(
                *_git_context(worktree_path),
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
