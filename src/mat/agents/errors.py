"""Errors raised by agent adapters."""

from __future__ import annotations


class AgentExecutionError(Exception):
    """Agent call failure with an adapter-provided retry recommendation."""

    def __init__(self, message: str, *, retry_recommended: bool = False) -> None:
        super().__init__(message)
        self.retry_recommended = retry_recommended
