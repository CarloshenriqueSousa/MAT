"""Revisão final da entrega (passo 8, D65).

Reúne, para a pessoa decidir sobre uma run em ``final_review``, tudo o que o MAT
registrou: diff do worktree contra a base, testes, alertas, erros e métricas. O
módulo só lê o banco e roda o Git endurecido do host sobre o worktree (D17): nenhum
código do repositório-alvo é executado. A CLI de hoje e a interface web (D57)
consomem o mesmo ``DeliveryReview``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from mat.db.connection import Database
from mat.gate.deterministic import split_diff

DiffProvider = Callable[[Path, str], Awaitable[str]]


class FileStat(BaseModel):
    path: str
    added: int = 0
    removed: int = 0
    status: str = "modified"  # added | modified | deleted | renamed


class TestSummary(BaseModel):
    __test__ = False  # not a pytest test class
    baseline_passed: int | None = None
    baseline_coverage_pct: float | None = None
    gate_passed: bool | None = None
    gate_tests_passed: int | None = None
    coverage_pct: float | None = None
    ruff_clean: bool | None = None
    tester_passed: bool | None = None
    tests_run: int | None = None
    tests_passed: int | None = None


class AttemptMetrics(BaseModel):
    attempt_id: str
    status: str
    tokens_used: int
    duration_ms: int
    failure_reason: str | None = None


class Metrics(BaseModel):
    attempts: int = 0
    tokens_used: int = 0
    duration_ms: int = 0
    dispatches: int = 0
    dispatch_retries: int = 0
    correction_rounds: int = 0
    per_attempt: list[AttemptMetrics] = Field(default_factory=list)


class DeliveryReview(BaseModel):
    run_id: str
    status: str
    execution_mode: str
    repo_path: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    work_branch: str | None = None
    worktree_path: str | None = None
    files: list[FileStat] = Field(default_factory=list)
    diff: str = ""
    tests: TestSummary = Field(default_factory=TestSummary)
    verdict: dict[str, Any] | None = None
    alerts: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)


def diff_stats(diff: str) -> list[FileStat]:
    """Linhas adicionadas/removidas por arquivo, a partir do diff unificado."""
    stats: list[FileStat] = []
    for section in split_diff(diff):
        added = removed = 0
        in_hunk = False
        for line in section.text.splitlines():
            if line.startswith("@@"):
                in_hunk = True
            elif in_hunk and line.startswith("+"):
                added += 1
            elif in_hunk and line.startswith("-"):
                removed += 1
        if section.old_path is None:
            status = "added"
        elif section.new_path is None:
            status = "deleted"
        elif section.old_path != section.new_path:
            status = "renamed"
        else:
            status = "modified"
        path = section.new_path or section.old_path or "?"
        stats.append(FileStat(path=path, added=added, removed=removed, status=status))
    return stats


def _json(raw: str | None) -> Any:
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


async def build_delivery_review(
    db: Database, run_id: str, *, diff_provider: DiffProvider | None = None
) -> DeliveryReview | None:
    """Monta a revisão da run (``None`` se ela não existe).

    ``diff_provider(worktree, base_sha)`` gera o diff atual do worktree; sem ele (ou sem
    worktree), a revisão sai sem diff e com um alerta.
    """
    run = await db.fetch_one("SELECT * FROM runs WHERE id = ?;", (run_id,))
    if run is None:
        return None
    review = DeliveryReview(
        run_id=run_id,
        status=run["status"],
        execution_mode=run["execution_mode"],
        repo_path=run["repo_path"],
        base_ref=run["base_ref"],
        base_sha=run["base_sha"],
        work_branch=run["work_branch"],
        worktree_path=run["worktree_path"],
    )

    await _add_diff(review, diff_provider)
    evidence = await db.fetch_all(
        "SELECT predicate, agent_type, content FROM evidence WHERE run_id = ? "
        "ORDER BY created_at, rowid;",
        (run_id,),
    )
    await _add_tests(db, review, evidence)
    _add_alerts(review, evidence)
    await _add_errors_and_metrics(db, review, evidence)
    return review


async def _add_diff(review: DeliveryReview, diff_provider: DiffProvider | None) -> None:
    worktree = Path(review.worktree_path) if review.worktree_path else None
    if worktree is None or review.base_sha is None:
        review.alerts.append("A run não tem worktree; não há diff para revisar.")
        return
    if not worktree.is_dir():
        review.alerts.append(f"O worktree não existe mais: {worktree}")
        return
    if diff_provider is None:
        return
    try:
        review.diff = await diff_provider(worktree, review.base_sha)
    except (RuntimeError, ValueError, OSError) as exc:
        review.alerts.append(f"Não foi possível gerar o diff do worktree: {exc}")
        return
    review.files = diff_stats(review.diff)
    if not review.files:
        review.alerts.append("O worktree não tem alterações em relação à base.")


async def _add_tests(db: Database, review: DeliveryReview, evidence: list) -> None:
    baseline = await db.fetch_one(
        "SELECT pytest_passed, coverage_pct FROM baselines WHERE run_id = ?;", (review.run_id,)
    )
    tests = review.tests
    if baseline is not None:
        tests.baseline_passed = baseline["pytest_passed"]
        tests.baseline_coverage_pct = baseline["coverage_pct"]

    final = [_json(row["content"]) for row in evidence if row["predicate"] == "final_review"]
    content = final[-1] if final and isinstance(final[-1], dict) else None
    if content is None:
        return
    gate = content.get("deterministic_gate") or {}
    tests.gate_passed = gate.get("passed")
    tests.gate_tests_passed = gate.get("pytest_passed")
    tests.coverage_pct = gate.get("coverage_pct")
    tests.ruff_clean = gate.get("ruff_clean")
    tester = content.get("tests") or {}
    tests.tester_passed = tester.get("passed")
    tests.tests_run = tester.get("tests_run")
    tests.tests_passed = tester.get("tests_passed")
    verdict = content.get("verdict")
    review.verdict = verdict if isinstance(verdict, dict) else None


def _add_alerts(review: DeliveryReview, evidence: list) -> None:
    for row in evidence:
        content = _json(row["content"]) or {}
        if not isinstance(content, dict):
            continue
        predicate = row["predicate"]
        if predicate == "test_change_approved":
            files = ", ".join(content.get("files") or [])
            review.alerts.append(f"Testes existentes alterados com a sua aprovação: {files}")
        elif predicate == "command_approved":
            review.alerts.append(f"Comando aprovado na run: {content.get('command')}")
        elif predicate == "command_rejected":
            review.alerts.append(f"Comando recusado na run: {content.get('command')}")
        elif predicate == "remediation_of":
            review.alerts.append(
                f"Run de remediação da base da run {content.get('source_run_id')}; refaça a "
                "tarefa original depois de publicar a correção."
            )
    if review.verdict:
        failed = [k for k, v in (review.verdict.get("criteria") or {}).items() if v is not True]
        if failed:
            review.alerts.append("Critérios do Verificador não atendidos: " + ", ".join(failed))
    tests = review.tests
    if (
        tests.coverage_pct is not None
        and tests.baseline_coverage_pct is not None
        and tests.coverage_pct < tests.baseline_coverage_pct
    ):
        review.alerts.append(
            f"Cobertura abaixo da base: {tests.coverage_pct}% < {tests.baseline_coverage_pct}%"
        )


async def _add_errors_and_metrics(db: Database, review: DeliveryReview, evidence: list) -> None:
    attempts = await db.fetch_all(
        "SELECT id, status, tokens_used, duration_ms, failure_reason FROM attempts "
        "WHERE run_id = ? ORDER BY created_at, rowid;",
        (review.run_id,),
    )
    metrics = review.metrics
    for att in attempts:
        metrics.per_attempt.append(
            AttemptMetrics(
                attempt_id=att["id"],
                status=att["status"],
                tokens_used=att["tokens_used"],
                duration_ms=att["duration_ms"],
                failure_reason=att["failure_reason"],
            )
        )
        if att["failure_reason"] and att["status"] == "failed":
            review.errors.append(f"Tentativa {att['id']} falhou: {att['failure_reason']}")
    metrics.attempts = len(attempts)
    metrics.tokens_used = sum(a.tokens_used for a in metrics.per_attempt)
    metrics.duration_ms = sum(a.duration_ms for a in metrics.per_attempt)

    count = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM dispatches d JOIN tasks t ON t.id = d.task_id "
        "WHERE t.run_id = ?;",
        (review.run_id,),
    )
    metrics.dispatches = count["n"] if count else 0
    for row in evidence:
        predicate = row["predicate"]
        if predicate == "dispatch_retry_recommended":
            metrics.dispatch_retries += 1
        elif predicate == "validation_correction":
            metrics.correction_rounds += 1
        elif predicate == "dispatch_failed":
            content = _json(row["content"]) or {}
            if isinstance(content, dict) and "Comando requer aprovação" not in str(
                content.get("error", "")
            ):
                review.errors.append(f"{row['agent_type']}: {content.get('error', '')}")
