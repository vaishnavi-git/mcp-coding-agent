"""Run budget (Phase 4): maximum tool calls, maximum model calls, maximum task cost.

Usage is read from the audit database rather than kept in memory, so a run's
budget covers every invocation of that run (start, each approval, each retry)
and survives restarts of the API process.

Cost = tokens x the configured model's price. Prices live in a JSON file
(``config/model_prices.json``) or in environment variables, never in code.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.storage.audit import AuditStore

# Default multipliers when only input/output prices are given (5-minute cache writes).
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


class PricingError(ValueError):
    """The price configuration is malformed."""


class BudgetExceeded(Exception):
    """A run limit is reached. ``limit`` is "cost", "llm_calls" or "tool_calls"."""

    def __init__(self, limit: str, message: str) -> None:
        super().__init__(message)
        self.limit = limit


def usd(amount: float) -> str:
    return f"${amount:.2f}" if amount >= 1 else f"${amount:.4f}"


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens for one model."""

    model: str
    input: float
    output: float
    cache_read: float
    cache_write: float
    source: str

    def cost(self, input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
        return (
            input_tokens * self.input
            + output_tokens * self.output
            + cache_read_tokens * self.cache_read
            + cache_write_tokens * self.cache_write
        ) / 1_000_000

    def cost_of(self, meta: dict[str, Any]) -> float:
        return self.cost(
            int(meta.get("input_tokens") or 0),
            int(meta.get("output_tokens") or 0),
            int(meta.get("cache_read_tokens") or 0),
            int(meta.get("cache_write_tokens") or 0),
        )


def _price(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise PricingError(f"{what} must be a non-negative number (got {value!r})")
    return float(value)


def load_pricing(model: str, prices_file: Path | None, overrides: dict[str, float | None] | None = None) -> Pricing | None:
    """Price for ``model``: environment overrides first, then the prices file. None if unknown."""
    overrides = overrides or {}
    if overrides.get("input") is not None and overrides.get("output") is not None:
        inp = _price(overrides["input"], "LLM_PRICE_INPUT_PER_MTOK")
        out = _price(overrides["output"], "LLM_PRICE_OUTPUT_PER_MTOK")
        read = overrides.get("cache_read")
        write = overrides.get("cache_write")
        return Pricing(
            model,
            inp,
            out,
            _price(read, "LLM_PRICE_CACHE_READ_PER_MTOK") if read is not None else inp * CACHE_READ_FACTOR,
            _price(write, "LLM_PRICE_CACHE_WRITE_PER_MTOK") if write is not None else inp * CACHE_WRITE_FACTOR,
            "environment",
        )
    if prices_file is None or not prices_file.exists():
        return None
    try:
        data = json.loads(prices_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PricingError(f"cannot read model prices from {prices_file}: {exc}") from exc
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        raise PricingError(f"{prices_file}: expected an object with a 'models' map")
    entry = models.get(model)
    if entry is None:
        return None
    if not isinstance(entry, dict) or "input" not in entry or "output" not in entry:
        raise PricingError(f"{prices_file}: model {model!r} needs 'input' and 'output' prices")
    where = f"{prices_file.name}: {model}"
    inp = _price(entry["input"], f"{where}.input")
    return Pricing(
        model,
        inp,
        _price(entry["output"], f"{where}.output"),
        _price(entry["cache_read"], f"{where}.cache_read") if "cache_read" in entry else inp * CACHE_READ_FACTOR,
        _price(entry["cache_write"], f"{where}.cache_write") if "cache_write" in entry else inp * CACHE_WRITE_FACTOR,
        prices_file.name,
    )


@dataclass(frozen=True)
class BudgetLimits:
    """None (or 0) switches a limit off."""

    max_cost_usd: float | None = None
    max_llm_calls: int | None = None
    max_tool_calls: int | None = None


@dataclass(frozen=True)
class Usage:
    llm_calls: int = 0  # successful model calls
    failed_llm_calls: int = 0  # attempts that returned an error (not billed)
    tool_calls: int = 0  # every call that reached an MCP server, retries included
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def minus(self, other: Usage) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls - other.llm_calls,
            "tool_calls": self.tool_calls - other.tool_calls,
            "cost_usd": round(self.cost_usd - other.cost_usd, 6),
        }


class RunBudget:
    def __init__(self, audit: AuditStore, run_id: str, limits: BudgetLimits, pricing: Pricing | None) -> None:
        self.audit = audit
        self.run_id = run_id
        self.limits = limits
        self.pricing = pricing

    def usage(self) -> Usage:
        return Usage(**self.audit.usage(self.run_id))

    def cost_of(self, meta: dict[str, Any]) -> float | None:
        if self.pricing is None or meta.get("error"):
            return None
        return self.pricing.cost_of(meta)

    def check_llm(self, purpose: str) -> None:
        """Raise BudgetExceeded if no further model call may start."""
        used = self.usage()
        max_calls = self.limits.max_llm_calls
        if max_calls and used.llm_calls >= max_calls:
            raise BudgetExceeded(
                "llm_calls",
                f"the run has made {used.llm_calls} model calls, its limit (MAX_LLM_CALLS_PER_RUN={max_calls}); stopped before the next one ({purpose})",
            )
        max_cost = self.limits.max_cost_usd
        if max_cost and used.cost_usd >= max_cost:
            raise BudgetExceeded(
                "cost",
                f"the run has spent {usd(used.cost_usd)} of its {usd(max_cost)} limit (MAX_TASK_COST_USD); stopped before the next model call ({purpose})",
            )

    def tool_calls_left(self) -> int | None:
        if not self.limits.max_tool_calls:
            return None
        return max(0, self.limits.max_tool_calls - self.usage().tool_calls)

    def check_tools(self, step: str) -> None:
        left = self.tool_calls_left()
        if left is not None and left <= 0:
            raise BudgetExceeded(
                "tool_calls",
                f"the run has made {self.limits.max_tool_calls} tool calls, its limit (MAX_TOOL_CALLS_PER_RUN={self.limits.max_tool_calls}); did not start '{step}'",
            )

    def exhausted_message(self) -> str:
        return (
            f"The run's tool-call limit (MAX_TOOL_CALLS_PER_RUN={self.limits.max_tool_calls}) is used up. "
            "Do not call more tools; reply with your answer now."
        )

    def snapshot(self) -> dict[str, Any]:
        used = self.usage()
        limits = {
            "max_cost_usd": self.limits.max_cost_usd or None,
            "max_llm_calls": self.limits.max_llm_calls or None,
            "max_tool_calls": self.limits.max_tool_calls or None,
        }
        return {
            **asdict(used),
            "cost_usd": round(used.cost_usd, 6),
            "limits": limits,
            "remaining": {
                "cost_usd": round(max(0.0, limits["max_cost_usd"] - used.cost_usd), 6) if limits["max_cost_usd"] else None,
                "llm_calls": max(0, limits["max_llm_calls"] - used.llm_calls) if limits["max_llm_calls"] else None,
                "tool_calls": max(0, limits["max_tool_calls"] - used.tool_calls) if limits["max_tool_calls"] else None,
            },
            "pricing": {"model": self.pricing.model, "source": self.pricing.source} if self.pricing else None,
        }
