"""Bounded LLM <-> MCP tool loop used inside workflow nodes.

The model only sees the tools the node allows. Every requested call is checked
(allowed? schema-valid? budget left?) before it reaches an MCP server, and
every result, including rejections, goes back to the model as a tool result.

Two limits apply (Phase 4):
- per step (``max_tool_calls``): every call the model requests counts,
  including rejected ones, so a model that keeps sending invalid calls stops;
- per run (``budget``): calls that would reach a server are refused once the
  run's tool-call limit is used up, and ``budget_exhausted`` is set.
Either way the model gets one more turn to answer without tools; if it still
asks for tools, the loop ends.

Arguments the tool's schema does not declare are rejected (Phase 5): MCP
servers ignore them, so a typo such as ``end_lines`` would otherwise be
dropped silently. Every rejected call is logged as a ``tool_rejected`` audit
event; calls that reach a server are logged by the hub.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from app.llm.base import LLMProvider, ToolDefinition, ToolResult
from app.mcp.client import MCPHub, ToolOutcome, ToolSpec
from app.reliability.budget import RunBudget

MAX_RESULT_CHARS = 30_000


@dataclass
class LoopCall:
    tool: str
    arguments: Any
    is_error: bool
    text: str


@dataclass
class LoopResult:
    final_text: str
    calls: list[LoopCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    hit_limit: bool = False  # the per-step limit stopped the loop
    budget_exhausted: bool = False  # the run's tool-call limit was reached


def validate_arguments(spec: ToolSpec, arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return "tool input must be a JSON object"
    schema = spec.input_schema or {"type": "object"}
    declared = schema.get("properties")
    if isinstance(declared, dict) and schema.get("additionalProperties") in (None, False):
        unknown = sorted(set(arguments) - set(declared))
        if unknown:
            accepted = ", ".join(declared) or "no arguments"
            return f"unknown argument{'s' if len(unknown) > 1 else ''} {', '.join(repr(k) for k in unknown)}; {spec.name} accepts: {accepted}"
    validator = jsonschema.validators.validator_for(schema)(schema)
    error = jsonschema.exceptions.best_match(validator.iter_errors(arguments))
    if error is None:
        return None
    where = "/".join(str(p) for p in error.absolute_path) or "input"
    return f"{where}: {error.message}"


def _clip(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n... [{len(text) - MAX_RESULT_CHARS} characters truncated]"


async def run_tool_loop(
    *,
    provider: LLMProvider,
    hub: MCPHub,
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolSpec],
    max_tool_calls: int,
    node: str,
    on_call: Callable[[ToolSpec, dict[str, Any], ToolOutcome], None] | None = None,
    budget: RunBudget | None = None,
) -> LoopResult:
    allowed = {spec.name: spec for spec in tools}
    definitions = [ToolDefinition(s.name, s.description, s.input_schema) for s in sorted(tools, key=lambda s: s.name)]
    messages = [provider.user_message(user_prompt)]
    result = LoopResult(final_text="")
    used = 0
    turns_after_budget = 0

    while True:
        response = await asyncio.to_thread(provider.chat, system_prompt, messages, definitions, purpose=node)
        messages.append(response.assistant_message)
        result.stop_reason = response.stop_reason
        if response.text.strip():
            result.final_text = response.text
        if response.stop_reason == "refusal" or not response.tool_calls:
            return result

        if used >= max_tool_calls or result.budget_exhausted:
            turns_after_budget += 1
            if turns_after_budget > 1:
                result.hit_limit = used >= max_tool_calls
                return result

        tool_results: list[ToolResult] = []
        for call in response.tool_calls:
            if response.stop_reason == "max_tokens":
                text, is_error = "Your response was cut off at max_tokens, so this tool input may be truncated; it was NOT executed. Send a smaller request.", True
                _log_rejection(hub, node, call, "truncated at max_tokens")
            elif used >= max_tool_calls:
                text, is_error = f"The tool-call limit for this step ({max_tool_calls}, MAX_TOOL_CALLS_PER_STEP) is used up. Do not call more tools; reply with your answer now.", True
                _log_rejection(hub, node, call, "per-step tool-call limit reached")
            else:
                used += 1
                text, is_error = await _execute(call, allowed, hub, node, budget, result, on_call)
            result.calls.append(LoopCall(call.name, call.arguments, is_error, text))
            tool_results.append(ToolResult(call.id, _clip(text), is_error))
        messages.append(provider.tool_results_message(tool_results))


async def _execute(call: Any, allowed: dict[str, ToolSpec], hub: MCPHub, node: str, budget: RunBudget | None, result: LoopResult, on_call: Any) -> tuple[str, bool]:
    if call.name not in allowed:
        _log_rejection(hub, node, call, "tool not available in this step")
        return f"Tool {call.name!r} is not available in this step. Available: {', '.join(sorted(allowed))}.", True
    spec = allowed[call.name]
    problem = validate_arguments(spec, call.arguments)
    if problem:
        _log_rejection(hub, node, call, f"invalid arguments: {problem}")
        return json.dumps({"INVALID_ARGUMENTS": problem, "received": call.arguments}, default=str), True
    if budget is not None and budget.tool_calls_left() == 0:
        result.budget_exhausted = True
        _log_rejection(hub, node, call, "run tool-call limit reached")
        return budget.exhausted_message(), True
    outcome = await hub.call(spec.name, call.arguments, node=node, initiator="llm")
    if on_call is not None:
        on_call(spec, call.arguments, outcome)
    return outcome.text, outcome.is_error


def _log_rejection(hub: MCPHub, node: str, call: Any, reason: str) -> None:
    """Record a tool call that never reached a server ("All tool calls must be logged")."""
    if hub.audit is not None and hub.run_id is not None:
        hub.audit.log_tool_rejection(hub.run_id, tool=str(call.name), node=node, reason=reason, arguments=call.arguments)
