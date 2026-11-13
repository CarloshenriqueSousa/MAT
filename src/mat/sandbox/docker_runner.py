"""Docker sandbox runner for executing pytest, ruff, and bash securely.

Enforces:
- --network none
- read-only root filesystem with writable /tmp
- CPU, memory, and PID limits
- host user UID:GID mapping
- command timeouts
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class DockerRunner:
    def __init__(
        self,
        image: str = "mat-sandbox:latest",
        memory_limit: str = "2g",
        cpu_limit: float = 2.0,
        timeout_seconds: int = 60,
    ) -> None:
        self.image = image
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.timeout_seconds = timeout_seconds

    async def run_command(
        self,
        worktree_path: Path,
        command: list[str],
        read_only: bool = False,
        timeout: int | None = None,
    ) -> CommandResult:
        """Executa um comando dentro do container sandbox com isolamento estrito."""
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        effective_timeout = timeout or self.timeout_seconds
        container_name = f"mat-command-{uuid4().hex}"

        mount_flag = "ro" if read_only else "rw"
        git_marker = worktree_path / ".git"
        mask_root: tempfile.TemporaryDirectory[str] | None = None
        git_mask: Path | None = None
        if git_marker.is_symlink():
            raise ValueError("O marcador .git do worktree não pode ser um symlink.")
        if git_marker.exists():
            mask_root = tempfile.TemporaryDirectory(prefix="mat-git-mask-")
            git_mask = Path(mask_root.name) / ".git"
            if git_marker.is_dir():
                git_mask.mkdir()
            else:
                git_mask.touch()

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=512m",
            "--memory",
            self.memory_limit,
            "--cpus",
            str(self.cpu_limit),
            "--pids-limit",
            "100",
            "--user",
            f"{uid}:{gid}",
            "-v",
            f"{worktree_path.resolve()}:/workspace:{mount_flag}",
            "-w",
            "/workspace",
            "-e",
            "PYTHONPATH=/workspace",
        ]
        if git_mask is not None:
            docker_cmd.extend(
                [
                    "--mount",
                    f"type=bind,source={git_mask},target=/workspace/.git,readonly",
                ]
            )
        docker_cmd.extend([self.image, *command])

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout
            )
            return CommandResult(
                exit_code=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
        except TimeoutError:
            # Killing the docker CLI alone may leave the container running.
            killer = await asyncio.create_subprocess_exec(
                "docker",
                "kill",
                "--signal",
                "KILL",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return CommandResult(
                exit_code=-1,
                stdout="",
                stderr=f"Comando excedeu o timeout de {effective_timeout}s",
                timed_out=True,
            )
        finally:
            if mask_root is not None:
                mask_root.cleanup()

    async def run_pytest(
        self, worktree_path: Path, extra_args: list[str] | None = None
    ) -> CommandResult:
        args = extra_args or []
        return await self.run_command(worktree_path, ["pytest", *args])

    async def run_ruff(self, worktree_path: Path) -> CommandResult:
        return await self.run_command(worktree_path, ["ruff", "check", "."])
