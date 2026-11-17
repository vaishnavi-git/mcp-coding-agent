"""A model and effort level per step, each call priced at the model it used, and the reports
that show where the cost goes (cost by step, cache hit rate, runs grouped by setup)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx2
import pytest

from app.config import MODEL_STEPS, PROJECT_ROOT, Settings
from app.evaluation import load_runs
from app.evaluation.metrics import describe_setup, run_metrics, summarize
from app.evaluation.report import render_markdown
from app.reliability.budget import PriceBook, PricingError, load_pricing
from tests.fakes import FakeRunner
from tests.test_anthropic_provider import API_KEY, message, provider_with
from tests.test_reliability_workflow import PASS, make_runner, scripted

pytestmark = pytest.mark.anyio

PRICES = PROJECT_ROOT / "config" / "model_prices.json"


# --------------------------------------------------------------------------- settings


def test_per_step_settings_map_onto_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("ANTHROPIC_MODEL_INVESTIGATE", "claude-haiku-5-5")
    monkeypatch.setenv("ANTHROPIC_MODEL_PUBLISH", " claude-haiku-5-5 ")
    monkeypatch.setenv("ANTHROPIC_EFFORT_PROPOSE", "high")
    monkeypatch.setenv("ANTHROPIC_MODEL_SUMMARY", "")  # empty means "use the main model"
    settings = Settings.from_env(env_file="/nonexistent/.env")
    assert settings.anthropic_model_by_step == {"investigate": "claude-haiku-5-5", "publish": "claude-haiku-5-5"}
    assert settings.models_by_purpose == {"investigate": "claude-haiku-5-5", "draft_publication": "claude-haiku-5-5", "check_publication": "claude-haiku-5-5"}
    assert settings.effort_by_purpose == {"propose": "high"}
    assert settings.models_in_use == ["claude-haiku-5-5", "claude-sonnet-5"]
    setup = settings.model_setup()
    assert setup["models_by_step"] == {"investigate": "claude-haiku-5-5", "publish": "claude-haiku-5-5"} and setup["effort_by_step"] == {"propose": "high"}


def test_every_model_call_belongs_to_a_step() -> None:
    purposes = {p for step in MODEL_STEPS.values() for p in step}
    workflow = (PROJECT_ROOT / "app" / "agents" / "coding_agent.py").read_text()
    import re

    used = set(re.findall(r'purpose="([a-z_]+)"', workflow)) | {"investigate", "propose"}  # the tool loops use the step name
    assert used == purposes


def test_the_provider_sends_each_step_its_own_model_and_effort() -> None:
    def echo(request: httpx2.Request) -> httpx2.Response:  # like the API, the response names the model that answered
        return httpx2.Response(200, json={**message([{"type": "text", "text": "{}"}]), "model": json.loads(request.content)["model"]})

    provider, requests, metrics = provider_with(
        echo,
        effort="medium",
        models_by_purpose={"investigate": "claude-haiku-5-5"},
        effort_by_purpose={"propose": "high"},
    )
    provider.generate("system", "user", purpose="plan")
    provider.generate("system", "user", purpose="investigate")
    provider.generate("system", "user", purpose="propose")
    sent = [(r["body"]["model"], r["body"].get("output_config", {}).get("effort")) for r in requests]
    assert sent == [("claude-sonnet-5", "medium"), ("claude-haiku-5-5", "medium"), ("claude-sonnet-5", "high")]
    assert [m["model"] for m in metrics] == ["claude-sonnet-5", "claude-haiku-5-5", "claude-sonnet-5"]  # so each is priced right


def test_from_settings_passes_the_step_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm.anthropic_provider import AnthropicProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("ANTHROPIC_MODEL_PLAN", "claude-haiku-5-5")
    provider = AnthropicProvider.from_settings(Settings.from_env(env_file="/nonexistent/.env"))
    assert provider.models_by_purpose == {"plan_searches": "claude-haiku-5-5", "plan": "claude-haiku-5-5"}


# --------------------------------------------------------------------------- prices


def test_haiku_is_priced_by_prompt_length() -> None:
    haiku = load_pricing("claude-haiku-5-5", PRICES)
    assert haiku is not None and (haiku.input, haiku.output, haiku.cache_read, haiku.cache_write) == (0.10, 0.50, 0.01, 0.125)
    assert haiku.cost(50_000, 1_000) == pytest.approx(0.0055)
    # over 100K prompt tokens, cache reads and writes included, every token is at the higher prices
    assert haiku.cost(10_000, 1_000, cache_read_tokens=95_000) == pytest.approx((10_000 * 0.50 + 1_000 * 2.50 + 95_000 * 0.05) / 1e6)
    assert haiku.cost(100_000, 0) == pytest.approx(0.01)  # exactly at the threshold: the lower prices


def test_a_malformed_long_context_price_is_refused(tmp_path: Path) -> None:
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"models": {"m": {"input": 1, "output": 2, "long_context": {"input": 3, "output": 4}}}}))
    with pytest.raises(PricingError, match="over_input_tokens"):
        load_pricing("m", prices)


def test_the_price_book_prices_each_model_and_overrides_only_the_main_one() -> None:
    book = PriceBook("claude-sonnet-5", PRICES, {"input": 9.0, "output": 9.0})
    assert book.get(None).input == 9.0 and book.get("claude-sonnet-5").source == "environment"
    assert book.get("claude-haiku-5-5").input == 0.10  # the override is for ANTHROPIC_MODEL only
    assert book.cost_of({"model": "claude-haiku-5-5", "input_tokens": 50_000}) == pytest.approx(0.005)
    assert book.cost_of({"model": "no-such-model", "input_tokens": 1}) is None
    assert book.missing(["claude-sonnet-5", "no-such-model"]) == ["no-such-model"]


async def test_each_call_is_priced_at_the_model_it_used(settings) -> None:
    mixed = dataclasses.replace(settings, anthropic_model_by_step={"investigate": "claude-haiku-5-5"})
    runner, audit = make_runner(mixed, scripted(), FakeRunner([PASS]))
    view = await runner.start(42)
    run_id = view["run"]["id"]
    calls = audit.llm_calls(run_id)
    priced = {(c["purpose"], c["model"], round(c["cost_usd"], 6)) for c in calls}
    # scripted calls use 1000 input and 200 output tokens: $0.004 on Sonnet 5, $0.0002 on Haiku 5.5
    assert ("investigate", "claude-haiku-5-5", 0.0002) in priced and ("plan", "claude-sonnet-5", 0.004) in priced
    assert all(model == "claude-haiku-5-5" for purpose, model, _ in priced if purpose == "investigate")
    assert view["usage"]["cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in calls))
    assert view["run"]["setup"]["models_by_step"] == {"investigate": "claude-haiku-5-5"}  # recorded with the run


async def test_a_cost_limit_needs_a_price_for_every_model_in_use(settings) -> None:
    unpriced = dataclasses.replace(settings, anthropic_model_by_step={"propose": "no-such-model"}, max_task_cost_usd=1.0)
    runner, _ = make_runner(unpriced, scripted())
    view = await runner.start(42)
    assert view["failure"]["code"] == "pricing_unknown" and "'no-such-model'" in view["failure"]["message"]


# --------------------------------------------------------------------------- reports


def test_setups_are_described_briefly() -> None:
    assert describe_setup(None, "claude-sonnet-5") == "claude-sonnet-5"  # runs recorded before setups were
    setup = {"model": "claude-sonnet-5", "effort": None, "models_by_step": {"investigate": "claude-haiku-5-5"}, "effort_by_step": {"propose": "high"}, "check_pr_summary": False}
    assert describe_setup(setup) == "claude-sonnet-5 (investigate: claude-haiku-5-5, propose effort high, no summary check)"
    assert describe_setup({"model": "claude-sonnet-5-5", "check_pr_summary": True}) == "claude-sonnet-5-5"


async def test_reports_show_cost_by_step_cache_hits_and_setups(settings) -> None:
    plain_runner, _ = make_runner(settings, scripted(), FakeRunner([PASS]))
    plain = (await plain_runner.start(42))["run"]["id"]
    await plain_runner.resume(plain, approved=True)  # the patch
    await plain_runner.resume(plain, approved=True)  # the publication, so the run finishes
    mixed_runner, _ = make_runner(dataclasses.replace(settings, anthropic_model_by_step={"investigate": "claude-haiku-5-5"}), scripted(), FakeRunner([PASS]))
    mixed = (await mixed_runner.start(42))["run"]["id"]
    await mixed_runner.resume(mixed, approved=True)
    await mixed_runner.resume(mixed, approved=True)

    records = await load_runs(settings, run_ids=[plain, mixed])
    rows = [run_metrics(r) for r in records]
    summary = summarize(rows)
    assert set(summary["by_setup"]) == {"claude-sonnet-5", "claude-sonnet-5 (investigate: claude-haiku-5-5)"}
    steps = summary["overall"]["cost_by_step"]
    assert {"plan_searches", "plan", "investigate", "propose", "draft_publication", "check_publication"} <= set(steps)
    assert sum(s["cost_usd"] for s in steps.values()) == pytest.approx(summary["overall"]["total_cost_usd"])
    assert sum(s["share"] for s in steps.values()) == pytest.approx(1.0)
    assert summary["overall"]["cache_hit_rate"] == 0.0  # the scripted model reports no cache reads
    assert summary["overall"]["summary_checks"] == 2 and summary["overall"]["summary_checks_corrected"] == 0
    text = render_markdown(summary, rows, source="test", selection="two runs")
    assert "## Cost by step" in text and "| `investigate` |" in text
    assert "| Cache hit rate (prompt tokens read from cache) | 0.0% |" in text.replace(" | 0.0% | 0.0%", "")
    assert "`claude-sonnet-5 (investigate: claude-haiku-5-5)`" in text and "| PR text corrected by the summary check | 0 of 2 |" in text
