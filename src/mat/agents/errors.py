"""Errors raised by agent adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mat.models.contracts import Baseline


class AgentExecutionError(Exception):
    """Agent call failure with an adapter-provided retry recommendation."""

    def __init__(self, message: str, *, retry_recommended: bool = False) -> None:
        super().__init__(message)
        self.retry_recommended = retry_recommended


class AmbiguousTaskError(AgentExecutionError):
    """The task cannot be reliably planned without human clarification."""


class AgentOutputError(AgentExecutionError):
    """The agent answered, but its output did not match the expected contract."""


class BaselineFailingError(AgentExecutionError):
    """A base já falha antes de qualquer edição (D43); a decisão volta para o humano."""

    def __init__(self, baseline: Baseline) -> None:
        self.baseline = baseline
        self.reasons = baseline.failure_reasons()
        super().__init__(" ".join(self.reasons) or "Baseline falhando.")
