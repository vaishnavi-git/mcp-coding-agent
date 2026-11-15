"""Provider-neutral LLM interface.

The agent depends only on this module. A provider turns these neutral
requests into its own API calls and maps its errors onto the classes below.
Conversation messages are opaque, JSON-serializable dicts built through the
provider (``user_message`` / ``tool_results_message``) and returned by it
(``LLMResponse.assistant_message``), so provider-specific content such as
thinking blocks is passed back unchanged without the agent inspecting it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]
MetricsSink = Callable[[dict[str, Any]], None]


class LLMError(Exception):
    """Base class. ``retryable`` tells the caller whether trying again can help."""

    retryable = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMAuthenticationError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class LLMTimeoutError(LLMError):
    retryable = True


class LLMAPIError(LLMError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message, status_code=status_code)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Any


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens" | "refusal" | ...
    model: str
    assistant_message: Message
    usage: dict[str, int] = field(default_factory=dict)


class LLMProvider(ABC):
    model: str
    # Receives metadata for every API attempt (model, purpose, latency, tokens,
    # stop reason, request id, error). Set by whoever owns the run's accounting.
    metrics_sink: MetricsSink | None = None

    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str, *, purpose: str = "generate") -> str:
        """Single-turn text generation."""

    @abstractmethod
    def generate_json(self, system_prompt: str, user_prompt: str, schema: dict[str, Any], *, purpose: str = "generate_json") -> dict[str, Any]:
        """Single-turn generation constrained to a JSON schema."""

    @abstractmethod
    def chat(self, system_prompt: str, messages: list[Message], tools: list[ToolDefinition], *, purpose: str = "chat") -> LLMResponse:
        """One turn of a tool-using conversation."""

    def user_message(self, text: str) -> Message:
        return {"role": "user", "content": text}

    @abstractmethod
    def tool_results_message(self, results: list[ToolResult]) -> Message:
        """Wrap tool results as the next message in the conversation."""
