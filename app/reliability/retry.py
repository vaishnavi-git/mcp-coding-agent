"""Retry limits with exponential backoff and jitter.

One policy is shared by model calls (``app.llm.reliable``) and MCP tool calls
(``app.mcp.client``). The policy only computes delays; callers do the waiting,
so tests can run without sleeping.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetryPolicy:
    """``max_retries`` retries after the first attempt.

    The delay before retry *n* (1-based) is ``base_delay * multiplier ** (n - 1)``,
    capped at ``max_delay`` and scaled by a random factor in ``[1 - jitter, 1]`` so
    that clients do not retry in lockstep. A server's ``Retry-After`` value is a
    lower bound; if it is longer than ``max_delay`` the call is not retried at all,
    because the wait would no longer be bounded.
    """

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    multiplier: float = 2.0
    jitter: float = 0.5
    random: Callable[[], float] = field(default=random.random, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must be >= 0")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")

    def backoff(self, retry_number: int) -> float:
        """Delay before retry ``retry_number`` (1-based), without Retry-After."""
        raw = min(self.max_delay, self.base_delay * self.multiplier ** (retry_number - 1))
        return raw * (1 - self.jitter * self.random())

    def next_delay(self, retry_number: int, retry_after: float | None = None) -> float | None:
        """Seconds to wait before retry ``retry_number``, or None to give up."""
        if retry_number > self.max_retries:
            return None
        delay = self.backoff(retry_number)
        if retry_after is not None:
            if retry_after > self.max_delay:
                return None
            delay = max(delay, retry_after)
        return delay

    def describe(self) -> str:
        return f"up to {self.max_retries} retries, backoff {self.base_delay:g}s doubling to at most {self.max_delay:g}s"


NO_RETRY = RetryPolicy(max_retries=0, base_delay=0, max_delay=0)
