"""Deterministic Gate: Enforces strict exit codes, linting, coverage baseline, and anti-cheat guards."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mat.models.contracts import (
    Baseline,
    DeterministicGateResult,
    Implementation,
    TestFileChange,
)
from mat.sandbox.docker_runner import DockerRunner

_SECTION_START = re.compile(r"^diff --git ", re.MULTILINE)
_BARE_SECTION_START = re.compile(r"^--- (?:a/|/dev/null$)", re.MULTILINE)
_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_TEST_FILE_NAME = re.compile(r"^(test_.*|.*_test)\.py$")
TEST_CHANGE_DIFF_LIMIT = 4000


@dataclass(frozen=True)
class FileDiff:
    """Uma seção ``diff --git`` com os caminhos antes (``None`` = novo) e depois."""

    old_path: str | None
    new_path: str | None
    text: str


def split_diff(diff: str) -> list[FileDiff]:
    """Divide um diff unificado do Git em seções por arquivo."""
    starts = [match.start() for match in _SECTION_START.finditer(diff)]
    if not starts:
        # Diff sem cabeçalho ``diff --git`` (ex.: colado à mão): cada ``--- `` abre uma seção.
        starts = [match.start() for match in _BARE_SECTION_START.finditer(diff)]
    if not starts:
        return []
    ends = [*starts[1:], len(diff)]
    return [_parse_section(diff[a:b]) for a, b in zip(starts, ends, strict=True)]


def _line(section: str, pattern: str) -> str | None:
    match = re.search(pattern, section, re.MULTILINE)
    return match.group(1) if match else None


def _parse_section(section: str) -> FileDiff:
    header = _HEADER.match(section.splitlines()[0])
    head_old, head_new = header.groups() if header else (None, None)

    if re.search(r"^(new file mode|--- /dev/null$)", section, re.MULTILINE):
        old = None
    else:
        old = _line(section, r"^--- a/(.+)$") or _line(section, r"^rename from (.+)$") or head_old
    if re.search(r"^(deleted file mode|\+\+\+ /dev/null$)", section, re.MULTILINE):
        new = None
    else:
        new = _line(section, r"^\+\+\+ b/(.+)$") or _line(section, r"^rename to (.+)$") or head_new
    return FileDiff(old_path=old, new_path=new, text=section)


def is_test_file(path: str) -> bool:
    """Arquivo de teste pelo diretório (``tests/``) ou pelo nome (``test_*.py``, ``*_test.py``)."""
    parts = PurePosixPath(path).parts
    return "tests" in parts[:-1] or bool(_TEST_FILE_NAME.match(parts[-1] if parts else ""))


def find_existing_test_changes(diff: str) -> list[TestFileChange]:
    """Alterações, remoções ou renomeações de arquivos de teste que já existiam na base.

    Cada uma vem com o hash da sua seção do diff: a aprovação humana vale só para
    aquele conteúdo exato (D42/D63).
    """
    changes: list[TestFileChange] = []
    for file_diff in split_diff(diff):
        if file_diff.old_path is None or not is_test_file(file_diff.old_path):
            continue
        text = file_diff.text
        changes.append(
            TestFileChange(
                path=file_diff.old_path,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                diff=text if len(text) <= TEST_CHANGE_DIFF_LIMIT else text[:TEST_CHANGE_DIFF_LIMIT] + "\n…",
            )
        )
    return changes


class DeterministicGate:
    def __init__(
        self,
        runner: DockerRunner,
        worktree_path: Path | None = None,
        diff_source: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self.runner = runner
        self.worktree_path = worktree_path
        # Quando presente, o diff é recalculado do worktree a cada avaliação, em vez de
        # confiar no diff que o Programador entregou (o Corretor ou uma pessoa podem ter
        # mudado o worktree depois).
        self.diff_source = diff_source

    async def check(
        self,
        baseline: Baseline,
        impl: Implementation,
        approved_test_changes: Mapping[str, str] | None = None,
    ) -> DeterministicGateResult:
        """Avalia a implementação no worktree configurado (interface do orquestrador)."""
        if self.worktree_path is None:
            raise ValueError("DeterministicGate.check exige worktree_path configurado.")
        diff = await self.diff_source() if self.diff_source is not None else impl.diff
        return await self.evaluate(
            self.worktree_path, baseline, diff, approved_test_changes=approved_test_changes
        )

    def check_anti_cheat(self, diff: str) -> str | None:
        """Trapaças que nunca são aprovadas: conftest, configuração de testes, skips.

        Alterar testes existentes não é trapaça: exige aprovação humana e é tratado por
        ``find_existing_test_changes``.
        """
        file_pairs = [(fd.old_path, fd.new_path) for fd in split_diff(diff)]
        protected_configs = {
            ".coveragerc",
            "coverage.toml",
            "pytest.ini",
            "tox.ini",
            "setup.cfg",
            "pyproject.toml",
        }
        for old_path, new_path in file_pairs:
            paths = [path for path in (old_path, new_path) if path]
            if any(path == "conftest.py" or path.endswith("/conftest.py") for path in paths):
                return "Detecção de trapaça: alterações em conftest.py não são permitidas."
            if any(Path(path).name in protected_configs for path in paths):
                return "Detecção de trapaça: alterações na configuração de testes/cobertura não são permitidas."

        # 1. Checa por adição de decorators de skip ou xfail
        if re.search(r"^\+\s*@pytest\.mark\.(skip|xfail)", diff, re.MULTILINE):
            return "Detecção de trapaça: diff adiciona @pytest.mark.skip ou @pytest.mark.xfail."

        if re.search(r"^\+.*(?:pytest\.skip\s*\(|unittest\.skip(?:If|Unless)?\s*\()", diff, re.MULTILINE):
            return "Detecção de trapaça: diff adiciona uma chamada de skip em teste."

        # 2. Checa se alterou configurações de cobertura para reduzir exigência
        if re.search(r"^\+.*fail_under\s*=\s*[0-9]+", diff, re.MULTILINE):
            return "Detecção de trapaça: alteração não autorizada nas regras de cobertura."

        return None

    async def evaluate(
        self,
        worktree_path: Path,
        baseline: Baseline,
        diff: str = "",
        *,
        approved_test_changes: Mapping[str, str] | None = None,
    ) -> DeterministicGateResult:
        """Avalia deterministamente o código no container sandbox."""
        # 1. Guarda anti-cheat
        cheat_error = self.check_anti_cheat(diff)
        if cheat_error:
            return DeterministicGateResult(
                passed=False,
                error_message=cheat_error,
            )

        # 2. Testes existentes alterados só passam com aprovação do conteúdo exato (D63).
        approved = approved_test_changes or {}
        pending = [
            change
            for change in find_existing_test_changes(diff)
            if approved.get(change.path) != change.sha256
        ]
        if pending:
            return DeterministicGateResult(
                passed=False,
                pending_test_changes=pending,
                error_message=(
                    "Alteração em testes existentes exige aprovação humana: "
                    + ", ".join(change.path for change in pending)
                ),
            )

        # 3. Executa linter ruff no container sandbox
        ruff_res = await self.runner.run_ruff(worktree_path)
        if not ruff_res.passed:
            return DeterministicGateResult(
                passed=False,
                ruff_clean=False,
                error_message=f"Ruff encontrou erros de linting:\n{ruff_res.stdout}\n{ruff_res.stderr}",
            )

        # 4. Executa pytest com cobertura no container sandbox
        pytest_res = await self.runner.run_pytest(
            worktree_path, extra_args=["-v", "--cov=."]
        )
        if not pytest_res.passed:
            return DeterministicGateResult(
                passed=False,
                ruff_clean=True,
                error_message=f"Pytest falhou com exit code {pytest_res.exit_code}:\n{pytest_res.stdout}\n{pytest_res.stderr}",
            )

        # Extrai contadores
        passed_match = re.search(r"(\d+) passed", pytest_res.stdout)
        total_passed = int(passed_match.group(1)) if passed_match else 0

        # Extrai cobertura
        coverage_pct = 0.0
        cov_match = re.search(r"TOTAL\s+\d+\s+\d+\s+([\d.]+)%", pytest_res.stdout)
        if cov_match:
            coverage_pct = float(cov_match.group(1))
        elif baseline.coverage_pct > 0:
            return DeterministicGateResult(
                passed=False,
                pytest_passed=total_passed,
                ruff_clean=True,
                error_message="Não foi possível obter a cobertura atual; gate reprovado por falta de evidência.",
            )

        # 5. Checa regressão de cobertura em relação ao baseline
        if coverage_pct < baseline.coverage_pct:
            return DeterministicGateResult(
                passed=False,
                pytest_passed=total_passed,
                coverage_pct=coverage_pct,
                ruff_clean=True,
                error_message=f"Regressão de cobertura: era {baseline.coverage_pct}%, agora é {coverage_pct}%.",
            )

        return DeterministicGateResult(
            passed=True,
            pytest_passed=total_passed,
            coverage_pct=coverage_pct,
            ruff_clean=True,
        )
