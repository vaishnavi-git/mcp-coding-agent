"""AnthropicProvider through the real anthropic SDK, with HTTP served by a mock transport."""

from __future__ import annotations

import json
import logging

import anthropic
import httpx2
import pytest

from app.config import Settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMAPIError, LLMAuthenticationError, LLMError, LLMRateLimitError, LLMTimeoutError, ToolDefinition, ToolResult

API_KEY = "sk-ant-api03-test-" + "k" * 24
SECRET_SOURCE = "PASSWORD_IN_SOURCE = 'hunter2'"


def message(content: list[dict], stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
    }


def provider_with(handler, **kwargs) -> tuple[AnthropicProvider, list[dict], list[dict]]:
    requests: list[dict] = []
    metrics: list[dict] = []

    def wrapped(request: httpx2.Request) -> httpx2.Response:
        requests.append({"headers": {k.lower(): v for k, v in request.headers.items()}, "body": json.loads(request.content)})
        return handler(request)

    client = anthropic.Anthropic(api_key=API_KEY, max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(wrapped)))
    provider = AnthropicProvider("claude-sonnet-5", client=client, metrics_sink=metrics.append, **kwargs)
    return provider, requests, metrics


def test_model_comes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    settings = Settings.from_env(env_file="/nonexistent/.env")
    assert settings.anthropic_model == "claude-opus-5"
    assert AnthropicProvider.from_settings(settings).model == "claude-opus-5"
    # No default in code: without ANTHROPIC_MODEL there is no model, and the provider says so.
    monkeypatch.delenv("ANTHROPIC_MODEL")
    unset = Settings.from_env(env_file="/nonexistent/.env")
    assert unset.anthropic_model == ""
    with pytest.raises(LLMError, match="ANTHROPIC_MODEL is not set"):
        AnthropicProvider.from_settings(unset)


def test_missing_api_key_fails_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings.from_env(env_file="/nonexistent/.env")
    with pytest.raises(LLMAuthenticationError, match="ANTHROPIC_API_KEY is not set"):
        AnthropicProvider.from_settings(settings)


def test_chat_sends_tools_and_parses_tool_calls() -> None:
    reply = message(
        [
            {"type": "thinking", "thinking": "", "signature": "sig-abc"},
            {"type": "text", "text": "Let me read the file."},
            {"type": "tool_use", "id": "toolu_1", "name": "read_repository_file", "input": {"path": "authkit/sessions.py"}},
        ],
        stop_reason="tool_use",
    )
    provider, requests, metrics = provider_with(lambda r: httpx2.Response(200, json=reply), effort="high")
    tools = [ToolDefinition("read_repository_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})]
    response = provider.chat("system prompt", [provider.user_message("Fix issue #42")], tools, purpose="investigate")

    body = requests[0]["body"]
    assert body["model"] == "claude-sonnet-5"
    assert body["tools"][0]["name"] == "read_repository_file"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert body["cache_control"] == {"type": "ephemeral"}
    headers = requests[0]["headers"]
    assert API_KEY in (headers.get("x-api-key", "") + headers.get("authorization", ""))  # header name varies by SDK version

    assert response.text == "Let me read the file."
    assert response.stop_reason == "tool_use"
    assert [(c.id, c.name, c.arguments) for c in response.tool_calls] == [("toolu_1", "read_repository_file", {"path": "authkit/sessions.py"})]
    # The thinking block is preserved verbatim for the next turn.
    assert response.assistant_message["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    follow_up = provider.tool_results_message([ToolResult("toolu_1", "file text", False)])
    assert follow_up["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file text", "is_error": False}
    assert metrics[0]["input_tokens"] == 120 and metrics[0]["cache_read_tokens"] == 100 and metrics[0]["purpose"] == "investigate"


def test_generate_skips_thinking_blocks_and_generate_json_parses() -> None:
    provider, requests, _ = provider_with(
        lambda r: httpx2.Response(200, json=message([{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": '{"summary": "ok"}'}]))
    )
    assert provider.generate("sys", "user") == '{"summary": "ok"}'
    assert provider.generate_json("sys", "user", {"type": "object"}) == {"summary": "ok"}
    assert requests[1]["body"]["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize(
    "response, error, fragment",
    [
        (httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}), LLMAuthenticationError, "ANTHROPIC_API_KEY"),
        (httpx2.Response(403, json={"type": "error", "error": {"type": "permission_error", "message": "no"}}), LLMAuthenticationError, "not allowed"),
        (httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": "model"}}), LLMAPIError, "ANTHROPIC_MODEL"),
        (httpx2.Response(429, headers={"retry-after": "17"}, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}), LLMRateLimitError, "retry after 17s"),
        (httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}), LLMAPIError, "(500)"),
        (httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad field"}}), LLMAPIError, "bad field"),
    ],
)
def test_api_errors_are_mapped(response: httpx2.Response, error: type, fragment: str) -> None:
    provider, _, metrics = provider_with(lambda r: response)
    with pytest.raises(error, match=fragment.replace("(", r"\(").replace(")", r"\)")) as info:
        provider.generate("sys", "user")
    assert metrics[0]["error"]
    if isinstance(info.value, LLMRateLimitError):
        assert info.value.retry_after == 17 and info.value.retryable
    if response.status_code == 500:
        assert info.value.retryable


def test_timeout_and_connection_errors() -> None:
    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    def refused(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(LLMTimeoutError):
        provider_with(timeout)[0].generate("sys", "user")
    with pytest.raises(LLMAPIError, match="connect") as info:
        provider_with(refused)[0].generate("sys", "user")
    assert info.value.retryable


def test_refusal_and_max_tokens_are_errors_for_single_turn_calls() -> None:
    provider, _, _ = provider_with(lambda r: httpx2.Response(200, json=message([], stop_reason="refusal")))
    with pytest.raises(LLMAPIError, match="declined"):
        provider.generate("sys", "user")
    provider, _, _ = provider_with(lambda r: httpx2.Response(200, json=message([{"type": "text", "text": '{"a"'}], stop_reason="max_tokens")))
    with pytest.raises(LLMAPIError, match="MAX_TOKENS"):
        provider.generate_json("sys", "user", {"type": "object"})


def test_logs_contain_metadata_but_no_key_or_source(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    provider, _, metrics = provider_with(lambda r: httpx2.Response(200, json=message([{"type": "text", "text": "ok"}])))
    provider.generate("system", f"Here is the file:\n{SECRET_SOURCE}", purpose="plan")
    app_logs = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert "llm_call purpose=plan model=claude-sonnet-5" in app_logs
    assert API_KEY not in caplog.text and SECRET_SOURCE not in app_logs
    assert set(metrics[0]) >= {"model", "latency_ms", "input_tokens", "output_tokens", "stop_reason", "request_id"}
    assert not any(SECRET_SOURCE in str(v) for v in metrics[0].values())


def test_sdk_retries_are_off_and_the_agent_retries_with_backoff_and_cost(tmp_path) -> None:
    """529 overloaded twice, then success. Every attempt is logged; only the success is billed."""
    from app.config import PROJECT_ROOT
    from app.llm.reliable import ReliableProvider
    from app.reliability.budget import BudgetLimits, RunBudget, load_pricing
    from app.reliability.retry import RetryPolicy
    from app.storage.audit import AuditStore

    replies = [
        httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
        httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, headers={"retry-after": "3"}),
        httpx2.Response(200, json=message([{"type": "text", "text": "done"}])),
    ]
    provider, requests, _ = provider_with(lambda r: replies.pop(0))
    audit = AuditStore(tmp_path / "agent.db")
    run_id = audit.create_run(1, "demo/repo", "claude-sonnet-5")
    budget = RunBudget(audit, run_id, BudgetLimits(max_cost_usd=1.0), load_pricing("claude-sonnet-5", PROJECT_ROOT / "config" / "model_prices.json"))
    provider.metrics_sink = lambda meta: audit.log_llm_call(run_id, {**meta, "cost_usd": budget.cost_of(meta)})
    sleeps: list[float] = []
    reliable = ReliableProvider(provider, policy=RetryPolicy(max_retries=3, base_delay=1, jitter=0), budget=budget, sleep=sleeps.append)

    assert reliable.generate("system", "user", purpose="summary") == "done"
    assert len(requests) == 3 and sleeps == [1, 3]  # backoff, then the server's Retry-After
    rows = audit.llm_calls(run_id)
    assert [r["error"] is None for r in rows] == [False, False, True]
    assert "overloaded (529)" in rows[0]["error"]
    # 120 input, 30 output, 100 cache-read tokens at $2 / $10 / $0.20 per MTok
    assert rows[2]["cost_usd"] == pytest.approx((120 * 2 + 30 * 10 + 100 * 0.2) / 1_000_000)
    assert rows[0]["cost_usd"] is None
