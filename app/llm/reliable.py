"""Retries and budget checks around any LLMProvider (Phase 4).

Every model call in the workflow goes through ``ReliableProvider``:

1. Before each attempt the run budget is checked (model-call count and cost).
   When it is used up, ``BudgetExceeded`` is raised and nothing is sent.
2. Errors the provider marks ``retryable`` (rate limits, overload, 5xx,
   timeouts, connection errors) are retried with exponential backoff, up to
   the policy's limit. ``Retry-After`` is honored. Other errors (bad key,
   invalid request, unknown model, refusal) are raised at once.
3. When retries run out, the original error is raised with the number of
   attempts added to its message.

Each attempt is logged by the provider itself (through ``metrics_sink``);
each retry is reported to ``on_retry`` (the runner records it as an event).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from app.reliability.budget import RunBudget
from app.reliability.retry import RetryPolicy

from .base import LLMError, LLMProvider, LLMResponse, Message, MetricsSink, ToolDefinition, ToolResult

T = TypeVar("T")
RetryHook = Callable[[dict[str, Any]], None]


class ReliableProvider(LLMProvider):
    def __init__(
        self,
        inner: LLMProvider,
        *,
        policy: RetryPolicy,
        budget: RunBudget | None = None,
        on_retry: RetryHook | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.inner = inner
        self.policy = policy
        self.budget = budget
        self.on_retry = on_retry
        self.sleep = sleep

    @property
    def model(self) -> str:  # type: ignore[override]
        return self.inner.model

    @property
    def metrics_sink(self) -> MetricsSink | None:  # type: ignore[override]
        return self.inner.metrics_sink

    def _call(self, purpose: str, fn: Callable[[], T]) -> T:
        retry = 0
        while True:
            if self.budget is not None:
                self.budget.check_llm(purpose)
            try:
                return fn()
            except LLMError as exc:
                delay = self.policy.next_delay(retry + 1, getattr(exc, "retry_after", None)) if exc.retryable else None
                if delay is None:
                    if retry:
                        exc.args = (f"{exc} Gave up after {retry + 1} attempts.",)
                    elif exc.retryable and getattr(exc, "retry_after", None):
                        exc.args = (f"{exc} Not retried: the server asked to wait longer than RETRY_MAX_DELAY_SECONDS ({self.policy.max_delay:g}s).",)
                    raise
                retry += 1
                if self.on_retry is not None:
                    self.on_retry({"purpose": purpose, "retry": retry, "delay_s": round(delay, 3), "error": f"{type(exc).__name__}: {exc}"})
                self.sleep(delay)

    # ------------------------------------------------------------------ LLMProvider
    def generate(self, system_prompt: str, user_prompt: str, *, purpose: str = "generate") -> str:
        return self._call(purpose, lambda: self.inner.generate(system_prompt, user_prompt, purpose=purpose))

    def generate_json(self, system_prompt: str, user_prompt: str, schema: dict[str, Any], *, purpose: str = "generate_json") -> dict[str, Any]:
        return self._call(purpose, lambda: self.inner.generate_json(system_prompt, user_prompt, schema, purpose=purpose))

    def chat(self, system_prompt: str, messages: list[Message], tools: list[ToolDefinition], *, purpose: str = "chat") -> LLMResponse:
        return self._call(purpose, lambda: self.inner.chat(system_prompt, messages, tools, purpose=purpose))

    def user_message(self, text: str) -> Message:
        return self.inner.user_message(text)

    def tool_results_message(self, results: list[ToolResult]) -> Message:
        return self.inner.tool_results_message(results)
