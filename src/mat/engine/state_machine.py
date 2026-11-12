"""State Machine definitions and transition rules for MAT."""

from __future__ import annotations

from enum import Enum


class Phase(str, Enum):
    INIT = "init"
    ANALYSIS = "analysis"
    AWAITING_HUMAN = "awaiting_human"
    PROGRAMMING = "programming"
    TESTING = "testing"
    VERIFICATION = "verification"
    COMPLETED = "completed"
    FAILED = "failed"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    AWAITING_HUMAN = "awaiting_human"
    CANCELLED = "cancelled"


class ExecutionMode(str, Enum):
    UNKNOWN = "unknown"
    SIMULATION = "simulation"
    REAL = "real"


class AttemptStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ESCALATED = "escalated"


# Regras formais de transição permitidas
VALID_PHASE_TRANSITIONS: dict[Phase, set[Phase]] = {
    Phase.INIT: {Phase.ANALYSIS, Phase.FAILED},
    Phase.ANALYSIS: {Phase.AWAITING_HUMAN, Phase.PROGRAMMING, Phase.FAILED},
    Phase.AWAITING_HUMAN: {Phase.PROGRAMMING, Phase.ANALYSIS, Phase.FAILED},
    Phase.PROGRAMMING: {Phase.TESTING, Phase.FAILED, Phase.AWAITING_HUMAN},
    Phase.TESTING: {
        Phase.VERIFICATION,
        Phase.PROGRAMMING,
        Phase.FAILED,
        Phase.AWAITING_HUMAN,
    },
    Phase.VERIFICATION: {
        Phase.COMPLETED,
        Phase.PROGRAMMING,
        Phase.ANALYSIS,
        Phase.FAILED,
        Phase.AWAITING_HUMAN,
    },
    Phase.COMPLETED: set(),
    Phase.FAILED: set(),
}


def assert_valid_transition(current: Phase, target: Phase) -> None:
    allowed = VALID_PHASE_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ValueError(
            f"Transição inválida de fase: '{current.value}' -> '{target.value}'. Permitidas: {[p.value for p in allowed]}"
        )
