"""Política de comandos do sandbox (D42/D64).

Os agentes só executam sem pedir aprovação os comandos de validação conhecidos
(pytest, Ruff) e comandos de leitura do worktree. Qualquer outro comando, ou uma
construção de shell que esconda o que vai rodar (substituição de comando,
redirecionamento para arquivo, here-doc, ``eval``), precisa de aprovação humana.

A política não substitui o sandbox: tudo continua rodando no container sem rede
(D16/D17). Ela limita o que o agente faz *dentro* do container sem que a pessoa veja.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# Programas liberados por padrão: validação conhecida e leitura do worktree.
DEFAULT_ALLOWED_PROGRAMS: tuple[str, ...] = (
    "pytest",
    "ruff",
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "find",
    "wc",
    "diff",
    "echo",
    "pwd",
    "sort",
    "uniq",
    "tree",
    "file",
    "stat",
    "true",
)
# ``python``/``python3`` só como ``-m pytest`` ou ``-m ruff``: ``-c`` e scripts rodam qualquer coisa.
_PYTHON = {"python", "python3"}
_PYTHON_MODULES = {"pytest", "ruff"}
_FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}
_SEPARATORS = {";", "&&", "||", "|", "&", "\n"}
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Construções que o parser não consegue avaliar com segurança.
_OPAQUE = (
    ("$(", "substituição de comando"),
    ("`", "substituição de comando"),
    ("<(", "substituição de processo"),
    (">(", "substituição de processo"),
    ("<<", "here-doc"),
)


class CommandApprovalRequired(Exception):
    """O agente pediu um comando fora da política; a pessoa precisa decidir (D64).

    Interrompe a chamada do agente (sem retry) para que o orquestrador pause a run.
    Fica aqui, e não em ``mat.agents.errors``, para não criar ciclo de import.
    """

    def __init__(self, command: str, reason: str) -> None:
        self.command = command
        self.reason = reason
        super().__init__(f"Comando requer aprovação humana ({reason}): {command}")


@dataclass(frozen=True)
class CommandCheck:
    allowed: bool
    reason: str = ""


@dataclass
class CommandPolicy:
    """Decide se um comando roda sem aprovação; guarda as decisões humanas da run."""

    allowed_programs: frozenset[str] = frozenset(DEFAULT_ALLOWED_PROGRAMS)
    # Comandos exatos aprovados (configuração ou decisão humana na run).
    approved: set[str] = field(default_factory=set)
    # Comandos exatos recusados pela pessoa nesta run: não rodam e não pausam de novo.
    denied: set[str] = field(default_factory=set)

    def check(self, command: str) -> CommandCheck:
        normalized = command.strip()
        if normalized in self.approved:
            return CommandCheck(True, "aprovado pela pessoa")
        if not normalized:
            return CommandCheck(False, "comando vazio")
        for marker, label in _OPAQUE:
            if marker in normalized:
                return CommandCheck(False, f"usa {label}")
        try:
            tokens = _tokenize(normalized)
        except ValueError as exc:
            return CommandCheck(False, f"não foi possível interpretar o comando ({exc})")

        for segment in _segments(tokens):
            reason = self._check_segment(segment)
            if reason:
                return CommandCheck(False, reason)
        return CommandCheck(True)

    def _check_segment(self, segment: list[str]) -> str | None:
        if not segment:
            return "segmento vazio entre operadores"
        if _ENV_ASSIGNMENT.match(segment[0]):
            return f"define variável de ambiente ({segment[0].split('=', 1)[0]})"
        for index, token in enumerate(segment):
            if token in {">", ">>", ">|", "&>", "&>>", "<", "<>"} or re.fullmatch(r"\d*[<>]&?", token):
                target = segment[index + 1] if index + 1 < len(segment) else ""
                if token.startswith(">") and target == "/dev/null":
                    continue
                if re.fullmatch(r"\d*>&", token) or token == "2>&1":
                    continue
                return "redireciona para arquivo"
        program = segment[0].rsplit("/", 1)[-1]
        if program in _PYTHON:
            if len(segment) >= 3 and segment[1] == "-m" and segment[2] in _PYTHON_MODULES:
                return None
            return "python só é liberado como `python -m pytest` ou `python -m ruff`"
        if program not in self.allowed_programs:
            return f"`{program}` não está entre os comandos conhecidos"
        if program == "find" and _FIND_ACTIONS.intersection(segment):
            return "find com ação que executa, grava ou apaga"
        return None


def _tokenize(command: str) -> list[str]:
    """Tokens do comando, com operadores separados e quebras de linha como separador."""
    tokens: list[str] = []
    for line_index, line in enumerate(command.split("\n")):
        if line_index:
            tokens.append("\n")
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens.extend(lexer)
    return tokens


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    # Um separador final (``cmd;``) não cria comando novo.
    if len(segments) > 1 and not segments[-1]:
        segments.pop()
    return segments
