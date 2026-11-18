"""Abertura de projeto e escolha da branch-base (passo 2 do roadmap, D56).

Só lê o repositório-alvo, sempre pelo Git endurecido de ``mat.sandbox.worktree``
(sem hooks, fsmonitor, filtros nem configuração global). Não faz ``fetch``: as
branches remotas refletem o último fetch feito pela pessoa responsável.
"""

from __future__ import annotations

import asyncio
import re
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from mat.sandbox.worktree import _git, _git_context, _git_env

_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
_FIELD_SEP = "\x00"  # %00 no formato do for-each-ref
_REF_FORMAT = "%(refname)%00%(objectname)%00%(committerdate:iso-strict)%00%(HEAD)%00%(subject)"


class ProjectError(Exception):
    """O projeto ou a branch escolhida não pode ser usado como base."""


class BaseMovedError(ProjectError):
    """A branch mudou de commit entre a listagem e a confirmação."""


class BranchKind(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class BranchRef(BaseModel):
    """Uma branch local ou remota e o commit exato para o qual ela aponta."""

    refname: str  # refs/heads/dev, refs/remotes/origin/dev
    name: str  # dev, origin/dev
    kind: BranchKind
    sha: str
    committed_at: str
    subject: str
    is_current: bool = False


class BaseSelection(BaseModel):
    """Branch-base confirmada por uma pessoa, presa a um commit imutável."""

    repo_path: Path
    refname: str
    name: str
    sha: str


async def _run_git(repo_path: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *_git(*_git_context(repo_path), *args, repository_path=repo_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_git_env(),
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ProjectError(f"git {args[0]} falhou: {stderr.decode('utf-8', errors='replace')}")
    return stdout.decode("utf-8", errors="replace")


def open_project(path: Path) -> Path:
    """Valida que ``path`` é a raiz de um repositório Git com working tree."""
    repo_path = path.expanduser().resolve()
    if not repo_path.is_dir():
        raise ProjectError(f"Diretório não encontrado: {repo_path}")
    try:
        _git_context(repo_path)
    except (RuntimeError, OSError) as exc:
        raise ProjectError(f"{repo_path} não é a raiz de um repositório Git: {exc}") from exc
    return repo_path


async def list_branches(repo_path: Path) -> list[BranchRef]:
    """Lista branches locais e remotas (sem ``fetch``), locais primeiro."""
    output = await _run_git(
        repo_path, "for-each-ref", f"--format={_REF_FORMAT}", "refs/heads", "refs/remotes"
    )
    branches: list[BranchRef] = []
    for line in output.splitlines():
        fields = line.split(_FIELD_SEP, 4)
        if len(fields) != 5:
            raise ProjectError(f"Saída inesperada do git for-each-ref: {line!r}")
        refname, sha, committed_at, head, subject = fields
        if refname.startswith("refs/remotes/") and refname.endswith("/HEAD"):
            continue  # ponteiro simbólico (origin/HEAD), não é uma branch
        if not _SHA_RE.fullmatch(sha):
            raise ProjectError(f"SHA inválido para {refname}: {sha!r}")
        if refname.startswith("refs/heads/"):
            kind, name = BranchKind.LOCAL, refname.removeprefix("refs/heads/")
        else:
            kind, name = BranchKind.REMOTE, refname.removeprefix("refs/remotes/")
        branches.append(
            BranchRef(
                refname=refname,
                name=name,
                kind=kind,
                sha=sha,
                committed_at=committed_at,
                subject=subject,
                is_current=head == "*",
            )
        )
    branches.sort(key=lambda b: (b.kind != BranchKind.LOCAL, b.name))
    return branches


async def confirm_base(repo_path: Path, branch: BranchRef) -> BaseSelection:
    """Prende a escolha ao commit exato; recusa se a branch andou desde a listagem."""
    current = (
        await _run_git(
            repo_path, "rev-parse", "--verify", "--end-of-options", f"{branch.refname}^{{commit}}"
        )
    ).strip()
    if current != branch.sha:
        raise BaseMovedError(
            f"{branch.name} mudou de {branch.sha[:12]} para {current[:12]} desde a listagem; "
            "revise e confirme de novo."
        )
    return BaseSelection(repo_path=repo_path, refname=branch.refname, name=branch.name, sha=current)
