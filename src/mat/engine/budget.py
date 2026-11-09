"""Token and timeout budget management per attempt."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class AttemptBudget:
    token_budget: int = 80000
    timeout_seconds: int = 300
    tokens_used: int = 0
    start_time: float = field(default_factory=time.time)

    def record_tokens(self, count: int) -> None:
        self.tokens_used += count

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.token_budget - self.tokens_used)

    @property
    def is_token_exhausted(self) -> bool:
        return self.tokens_used >= self.token_budget

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def is_timed_out(self) -> bool:
        return self.elapsed_seconds >= self.timeout_seconds

    @property
    def is_exhausted(self) -> bool:
        return self.is_token_exhausted or self.is_timed_out
