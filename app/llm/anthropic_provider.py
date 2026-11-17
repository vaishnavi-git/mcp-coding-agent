"""Anthropic implementation of LLMProvider (official ``anthropic`` SDK, Messages API).

- The model comes from settings (``ANTHROPIC_MODEL``, or ``ANTHROPIC_MODEL_<STEP>`` for one
  step), never from code. The model each call used is in its metadata, so it is priced right.
- SDK errors are mapped onto the neutral LLM error classes.
- Each call logs metadata only (model, purpose, latency, tokens, stop reason,
  request id). Prompts, source code and the API key are never logged.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import anthropic

from app.config import Settings

from .base import (
    LLMAPIError,
    LLMAuthenticationError,
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    Message,
    MetricsSink,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

log = logging.getLogger("app.llm")


class AnthropicProvider(LLMProvider):
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        max_tokens: int = 16000,
        effort: str | None = None,
        thinking: str = "adaptive",
        timeout_seconds: float = 120.0,
        max_retries: int = 0,
        client: anthropic.Anthropic | None = None,
        metrics_sink: MetricsSink | None = None,
        models_by_purpose: dict[str, str] | None = None,
        effort_by_purpose: dict[str, str] | None = None,
    ) -> None:
        if not model:
            raise ValueError("model is required (set ANTHROPIC_MODEL)")
        self.model = model
        self.models_by_purpose = dict(models_by_purpose or {})
        self.effort_by_purpose = dict(effort_by_purpose or {})
        self.max_tokens = max_tokens
        self.effort = effort
        self.thinking = thinking
        self.metrics_sink = metrics_sink
        if client is not None:
            self.client = client
        else:
            try:
                # With no explicit key the SDK resolves ANTHROPIC_API_KEY / auth profiles itself.
                self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=max_retries) if api_key else anthropic.Anthropic(timeout=timeout_seconds, max_retries=max_retries)
            except anthropic.AnthropicError as exc:
                raise LLMAuthenticationError("No Anthropic credentials found. Set ANTHROPIC_API_KEY in the environment or .env file.") from exc

    @classmethod
    def from_settings(cls, settings: Settings, metrics_sink: MetricsSink | None = None) -> AnthropicProvider:
        """The SDK's own retries are off: ``app.llm.reliable`` retries with backoff
        (ANTHROPIC_MAX_RETRIES), so every attempt is logged and checked against the budget."""
        if not settings.anthropic_api_key:
            # Fail before any request is sent, with an actionable message.
            raise LLMAuthenticationError("ANTHROPIC_API_KEY is not set. Add it to your environment or .env file (see .env.example).")
        if not settings.anthropic_model:
            raise LLMError("ANTHROPIC_MODEL is not set. Add it to your environment or .env file (see .env.example).")
        return cls(
            settings.anthropic_model,
            api_key=settings.anthropic_api_key,
            max_tokens=settings.anthropic_max_tokens,
            effort=settings.anthropic_effort,
            thinking=settings.anthropic_thinking,
            timeout_seconds=settings.anthropic_timeout_seconds,
            max_retries=0,
            metrics_sink=metrics_sink,
            models_by_purpose=settings.models_by_purpose,
            effort_by_purpose=settings.effort_by_purpose,
        )

    # ------------------------------------------------------------------ core
    def _create(self, purpose: str, **request: Any) -> Any:
        model = self.models_by_purpose.get(purpose) or self.model
        effort = self.effort_by_purpose.get(purpose) or self.effort
        params: dict[str, Any] = {"model": model, "max_tokens": self.max_tokens, **request}
        if self.thinking == "adaptive":
            params["thinking"] = {"type": "adaptive"}
        if effort:
            params["output_config"] = {**params.get("output_config", {}), "effort": effort}
        meta: dict[str, Any] = {"purpose": purpose, "model": model}
        start = time.monotonic()
        try:
            response = self.client.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            raise self._fail(meta, start, LLMAuthenticationError("Anthropic rejected the API key (401). Check ANTHROPIC_API_KEY.", status_code=401), exc) from exc
        except anthropic.PermissionDeniedError as exc:
            raise self._fail(meta, start, LLMAuthenticationError(f"The API key is not allowed to use model {model!r} (403).", status_code=403), exc) from exc
        except anthropic.NotFoundError as exc:
            raise self._fail(meta, start, LLMAPIError(f"Model {model!r} was not found (404). Check ANTHROPIC_MODEL and any ANTHROPIC_MODEL_<STEP>.", status_code=404), exc) from exc
        except anthropic.RateLimitError as exc:
            retry_after = _retry_after(exc)
            msg = "Anthropic rate limit reached (429)" + (f"; retry after {retry_after:.0f}s." if retry_after else ".")
            raise self._fail(meta, start, LLMRateLimitError(msg, retry_after=retry_after), exc) from exc
        except anthropic.APITimeoutError as exc:
            raise self._fail(meta, start, LLMTimeoutError("The Anthropic API request timed out."), exc) from exc
        except anthropic.APIConnectionError as exc:
            raise self._fail(meta, start, LLMAPIError("Could not connect to the Anthropic API.", retryable=True), exc) from exc
        except anthropic.BadRequestError as exc:
            raise self._fail(meta, start, LLMAPIError(f"Anthropic rejected the request (400): {exc.message}", status_code=400), exc) from exc
        except anthropic.APIStatusError as exc:
            code = exc.status_code
            label = "overloaded" if code == 529 else "error"
            raise self._fail(
                meta, start, LLMAPIError(f"Anthropic API {label} ({code}).", status_code=code, retryable=code >= 500, retry_after=_retry_after(exc)), exc
            ) from exc
        usage = response.usage
        meta.update(  # "model" stays the requested name: that is what the price file and the reports use
            latency_ms=int((time.monotonic() - start) * 1000),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
            stop_reason=response.stop_reason,
            request_id=getattr(response, "_request_id", None),
        )
        self._emit(meta)
        return response

    def _fail(self, meta: dict[str, Any], start: float, error: LLMError, cause: Exception) -> LLMError:
        meta.update(
            latency_ms=int((time.monotonic() - start) * 1000),
            error=f"{type(error).__name__}: {error}",
            request_id=getattr(cause, "request_id", None),
        )
        self._emit(meta)
        error.__cause__ = cause
        return error

    def _emit(self, meta: dict[str, Any]) -> None:
        log.info(
            "llm_call purpose=%s model=%s latency_ms=%s input_tokens=%s output_tokens=%s stop_reason=%s request_id=%s error=%s",
            meta.get("purpose"), meta.get("model"), meta.get("latency_ms"), meta.get("input_tokens"),
            meta.get("output_tokens"), meta.get("stop_reason"), meta.get("request_id"), meta.get("error"),
        )
        if self.metrics_sink is not None:
            self.metrics_sink(dict(meta))

    # ------------------------------------------------------------------ interface
    def generate(self, system_prompt: str, user_prompt: str, *, purpose: str = "generate") -> str:
        response = self._create(purpose, system=system_prompt, messages=[{"role": "user", "content": user_prompt}])
        _check_stop(response)
        return _text(response)

    def generate_json(self, system_prompt: str, user_prompt: str, schema: dict[str, Any], *, purpose: str = "generate_json") -> dict[str, Any]:
        response = self._create(
            purpose,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        _check_stop(response)
        text = _text(response)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMAPIError(f"The model did not return valid JSON for {purpose}.") from exc

    def chat(self, system_prompt: str, messages: list[Message], tools: list[ToolDefinition], *, purpose: str = "chat") -> LLMResponse:
        response = self._create(
            purpose,
            system=system_prompt,
            messages=messages,
            tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
            # Automatic prompt caching: the stable tools + system + transcript prefix is reused across turns.
            cache_control={"type": "ephemeral"},
        )
        calls = [ToolCall(block.id, block.name, block.input) for block in response.content if block.type == "tool_use"]
        usage = {
            "input_tokens": response.usage.input_tokens or 0,
            "output_tokens": response.usage.output_tokens or 0,
        }
        return LLMResponse(
            text=_text(response),
            tool_calls=calls,
            stop_reason=response.stop_reason or "end_turn",
            model=response.model,
            # Content is passed back unchanged on the next turn (thinking blocks included).
            assistant_message={"role": "assistant", "content": [b.model_dump(mode="json", exclude_none=True) for b in response.content]},
            usage=usage,
        )

    def tool_results_message(self, results: list[ToolResult]) -> Message:
        return {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.tool_call_id, "content": r.content or "(empty)", "is_error": r.is_error}
                for r in results
            ],
        }


def _text(response: Any) -> str:
    # Thinking blocks may come first, so never assume content[0] is text.
    return "".join(block.text for block in response.content if block.type == "text")


def _check_stop(response: Any) -> None:
    if response.stop_reason == "refusal":
        raise LLMAPIError("The model declined this request (stop_reason=refusal).")
    if response.stop_reason == "max_tokens":
        raise LLMAPIError("The response hit ANTHROPIC_MAX_TOKENS before finishing; increase it.")


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value else None
    except (AttributeError, ValueError):
        return None
