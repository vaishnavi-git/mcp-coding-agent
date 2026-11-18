"""Benchmark mode: where it refuses to run, the evaluator's review policy, a scripted run judged
by the hidden acceptance tests, and the acceptance tests themselves on right and wrong fixes."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.agents.runner import AgentRunner
from app.api.main import ApprovalDecision
from app.config import PROJECT_ROOT, Settings
from app.evaluation import load_runs, run_metrics, summarize
from app.evaluation.benchmark import (
    EVALUATOR,
    BenchmarkRefused,
    check_allowed,
    evaluator_decision,
    load_tasks,
    run_acceptance,
    run_benchmark,
    task_settings,
)
from app.evaluation.report import render_markdown
from app.evaluation.trials import STARTING_POINTS, prepare_trial
from app.languages import SAMPLES
from app.mcp.sandbox_server import DockerRunner
from app.sample_repo import setup_sample_repo
from app.storage.audit import AuditStore
from tests.conftest import make_settings
from tests.fakes import LocalPytestRunner, ScriptedProvider, as_configured, inprocess_hub_factory
from tests.test_agent_workflow import FULL_FIX, INVESTIGATE, PLAN, REGRESSION_TEST
from tests.test_languages import _image_ready

pytestmark = pytest.mark.anyio

TASKS = {t.id: t for t in load_tasks()}


@pytest.fixture
def workspaces(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


@pytest.fixture
def base(tmp_path: Path, workspaces: Path) -> Settings:
    return make_settings(workspaces / "sample-repo", tmp_path / "data")


# --------------------------------------------------------------------------- where it runs


def test_every_task_has_its_acceptance_test_and_a_valid_language() -> None:
    assert set(TASKS) == {f"{lang}-{n}" for lang in ("python", "javascript", "java") for n in (42, 43)} | {"shop-7", "shop-8", "shop-9"}
    for task in TASKS.values():
        assert task.acceptance.is_file() and task.language in ("python", "javascript", "java")
        assert task.sample is None or task.sample in SAMPLES
    with pytest.raises(ValueError, match="unknown task"):
        load_tasks(only=["python-99"])


def test_it_refuses_outside_fixture_mode_and_the_bundled_samples(base: Settings, workspaces: Path, tmp_path: Path) -> None:
    for task in TASKS.values():
        check_allowed(task_settings(base, task, workspaces), workspaces)  # every task's sample is allowed
    with pytest.raises(BenchmarkRefused, match="GITHUB_MODE=fixture"):
        check_allowed(dataclasses.replace(base, github_mode="api"), workspaces)
    with pytest.raises(BenchmarkRefused, match="only runs on the bundled sample repositories"):
        check_allowed(dataclasses.replace(base, repo_path=tmp_path / "my-real-project"), workspaces)
    with pytest.raises(BenchmarkRefused, match="only runs on the bundled sample repositories"):
        check_allowed(dataclasses.replace(base, repo_path=workspaces / "sample-repo" / ".." / "elsewhere"), workspaces)


async def test_run_benchmark_refuses_before_touching_anything(base: Settings, workspaces: Path) -> None:
    with pytest.raises(BenchmarkRefused):
        await run_benchmark(dataclasses.replace(base, github_mode="api"), [TASKS["python-42"]], workspaces=workspaces)
    assert not workspaces.exists()  # nothing was set up


def test_the_script_refuses_in_api_mode(tmp_path: Path) -> None:
    env = {**os.environ, "GITHUB_MODE": "api", "DATA_DIR": str(tmp_path / "data"), "ANTHROPIC_MODEL": "claude-sonnet-5"}
    proc = subprocess.run([sys.executable, "scripts/run_benchmark.py", "--tasks", "python-42", "--env-file", str(tmp_path / "none.env")], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and "Refused" in proc.stderr and "GITHUB_MODE=fixture" in proc.stderr


def test_the_api_cannot_answer_as_the_evaluator() -> None:
    decision = ApprovalDecision.model_validate({"approved": True, "decided_by": EVALUATOR})
    assert "decided_by" not in decision.model_dump()  # API decisions are always recorded as a person's


# --------------------------------------------------------------------------- review policy


def test_the_evaluator_policy() -> None:
    assert evaluator_decision({"kind": "plan_approval"}, 0) == (True, {})
    assert evaluator_decision({"kind": "patch_approval"}, 0) == (True, {})
    assert evaluator_decision({"kind": "recovery"}, 0) == (True, {})  # one more attempt
    assert evaluator_decision({"kind": "recovery"}, 1) == (False, {})  # then stop
    assert evaluator_decision({"kind": "publish_approval"}, 0) == (True, {"publish_up_to": "pull_request"})
    with pytest.raises(BenchmarkRefused):
        evaluator_decision({"kind": "something_new"}, 0)


# --------------------------------------------------------------------------- a scripted benchmark run


async def test_a_benchmark_run_is_reviewed_by_the_evaluator_and_judged_by_hidden_tests(base: Settings, workspaces: Path) -> None:
    provider = ScriptedProvider(
        plan=PLAN,
        turns={
            "investigate": list(INVESTIGATE),
            "propose": [[("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX}), ("propose_patch", {"path": "tests/test_sessions.py", "patch": REGRESSION_TEST})], "Fix rotation."],
        },
    )

    def make(settings: Settings) -> AgentRunner:
        audit = AuditStore(settings.database_path)
        return AgentRunner(settings, audit=audit, provider_factory=lambda run_id: as_configured(provider, settings), hub_factory=inprocess_hub_factory(settings, LocalPytestRunner(), audit))

    reported: list[dict] = []
    result = await run_benchmark(base, [TASKS["python-42"]], workspaces=workspaces, make_runner=make, sandbox=LocalPytestRunner(), report=reported.append)
    [run] = result["runs"]
    assert reported == [run] and run["status"] == "published" and result["stopped"] is None
    assert run["acceptance"]["status"] == "PASSED" and run["acceptance"]["summary"].startswith("3 passed")
    assert result["total_cost_usd"] == pytest.approx(run["cost_usd"])

    settings = task_settings(base, TASKS["python-42"], workspaces)
    [record] = await load_runs(settings, run_ids=[run["run_id"]])
    state = record.state
    decisions = [*state["approvals"], *state["publication_approvals"]]
    assert decisions and all(d["decided_by"] == EVALUATOR for d in decisions)  # never recorded as a person
    assert not (settings.repo_path / "tests" / "test_acceptance_42.py").exists()  # the hidden test never enters the repository
    row = run_metrics(record)
    assert (row["reviewer"], row["acceptance"]) == (EVALUATOR, "PASSED")
    summary = summarize([row])
    assert summary["overall"]["acceptance_pass_rate"] == 1.0 and summary["overall"]["reviewers"] == {EVALUATOR: 1}
    text = render_markdown(summary, [row], source="test", selection="one benchmark run")
    assert "| Hidden acceptance tests passed (benchmark runs) | 100.0% (1 of 1) |" in text and "| Checkpoints answered by | evaluator: 1 |" in text


async def test_the_total_cost_limit_stops_before_the_next_run(base: Settings, workspaces: Path) -> None:
    result = await run_benchmark(base, [TASKS["python-42"]], runs=3, max_total_cost=0.0, workspaces=workspaces)
    assert result["runs"] == [] and "stopped after spending $0.0000" in result["stopped"]


# --------------------------------------------------------------------------- the acceptance tests themselves

FIXES = {
    "python": ("authkit/sessions.py", STARTING_POINTS[("python", 43)][0]),
    "javascript": ("src/sessions.js", STARTING_POINTS[("javascript", 43)][0]),
    "java": ("src/main/java/authkit/SessionStore.java", STARTING_POINTS[("java", 43)][0]),
}
# A wrong fix for #42: the new refresh token is stored, but the used one is never revoked.
HALF_FIXES = {
    "python": ("        self._by_refresh.pop(refresh_token, None)\n", ""),
    "javascript": ("    this._byRefresh.delete(refreshToken);\n", ""),
    "java": ("        byRefresh.remove(refreshToken);\n", ""),
}
# A wrong change for #43: logout throws for a token it doesn't know.
STRICT_LOGOUT = {
    "python": ("        session = self._by_access.pop(access_token, None)\n", "        session = self._by_access.pop(access_token)\n"),
    "javascript": (
        "    const session = this._byAccess.get(accessToken);\n    this._byAccess.delete(accessToken);\n",
        "    const session = this._byAccess.get(accessToken);\n    if (session === undefined) throw new InvalidToken('unknown access token');\n    this._byAccess.delete(accessToken);\n",
    ),
    "java": ("        Session session = byAccess.remove(accessToken);\n", "        Session session = byAccess.remove(accessToken);\n        if (session == null) throw new InvalidTokenException(\"unknown access token\");\n"),
}


def variant(tmp_path: Path, language: str, name: str, *changes: tuple[str, str]) -> Path:
    repo = setup_sample_repo(tmp_path / name, language=language, with_origin=False)
    rel = FIXES[language][0]
    text = (repo / rel).read_text()
    for old, new in changes:
        assert text.count(old) == 1, (language, old)
        text = text.replace(old, new)
    (repo / rel).write_text(text)
    return repo


def judge(tmp_path: Path, language: str, sandbox) -> dict[str, dict[str, str]]:
    _, (_, bug, fix, _) = FIXES[language]
    half_old, half_new = HALF_FIXES[language]
    repos = {
        "as shipped": variant(tmp_path, language, "shipped"),
        "fixed": variant(tmp_path, language, "fixed", (bug, fix)),
        "half fixed": variant(tmp_path, language, "half", (bug, fix.replace(half_old, half_new))),
        "strict logout": variant(tmp_path, language, "strict", (bug, fix), STRICT_LOGOUT[language]),
    }
    return {
        issue: {name: run_acceptance(TASKS[f"{language}-{issue}"], repo, sandbox)["status"] for name, repo in repos.items()}
        for issue in (42, 43)
    }


EXPECTED = {
    42: {"as shipped": "FAILED", "fixed": "PASSED", "half fixed": "FAILED", "strict logout": "PASSED"},
    43: {"as shipped": "PASSED", "fixed": "PASSED", "half fixed": "PASSED", "strict logout": "FAILED"},  # #43 already works as shipped
}


def test_python_acceptance_tests_tell_right_fixes_from_wrong_ones(tmp_path: Path) -> None:
    assert judge(tmp_path, "python", LocalPytestRunner()) == EXPECTED


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_javascript_acceptance_tests_tell_right_fixes_from_wrong_ones(tmp_path: Path) -> None:
    assert judge(tmp_path, "javascript", LocalPytestRunner()) == EXPECTED


@pytest.mark.docker
@pytest.mark.skipif(not _image_ready("mcp-agent-sandbox-java:latest"), reason="needs Docker and mcp-agent-sandbox-java:latest")
def test_java_acceptance_tests_tell_right_fixes_from_wrong_ones(tmp_path: Path) -> None:
    assert judge(tmp_path, "java", DockerRunner("mcp-agent-sandbox-java:latest", timeout_seconds=120)) == EXPECTED


def test_the_tasks_file_is_valid_json_with_known_commands() -> None:
    data = json.loads((PROJECT_ROOT / "examples" / "eval" / "tasks.json").read_text())
    for task in data["tasks"]:
        assert task["command"].split()[0] in ("pytest", "node", "mvn")



# --------------------------------------------------------------------------- the harder shop tasks

# Right and wrong fixes for each shop issue: (file, old text, new text).
SHOP_FIXES = {
    7: {
        "right": ("shop/checkout.py", """    tax = ZERO
    for line in cart.lines:
        if line.product.taxable:
            tax += line.total.percent(TAX_RATE)
""", """    taxable = sum((line.total for line in cart.lines if line.product.taxable), ZERO)
    tax = taxable.percent(TAX_RATE)
"""),
        # what the reporter suggested: round down in Money.percent
        "wrong": ("shop/money.py", "from decimal import ROUND_HALF_UP, Decimal", "from decimal import ROUND_DOWN as ROUND_HALF_UP, Decimal"),
    },
    8: {
        "right": ("shop/cart.py", """    def apply_coupon(self, code: str) -> None:
        self.coupons.append(code)""", """    def apply_coupon(self, code: str) -> None:
        code = code.strip().upper()
        if code not in self.coupons:
            self.coupons.append(code)"""),
        # duplicates refused, but spelling not normalized
        "wrong": ("shop/cart.py", """    def apply_coupon(self, code: str) -> None:
        self.coupons.append(code)""", """    def apply_coupon(self, code: str) -> None:
        if code not in self.coupons:
            self.coupons.append(code)"""),
    },
    9: {
        "right": ("shop/discounts.py", "    free = len(units) // 3\n", "    free = len(units) // 3\n    units.sort()\n"),
        # sorted, but the wrong way round
        "wrong": ("shop/discounts.py", "    free = len(units) // 3\n", "    free = len(units) // 3\n    units.sort(reverse=True)\n"),
    },
}


def shop_variant(tmp_path: Path, name: str, change: tuple[str, str, str] | None) -> Path:
    repo = setup_sample_repo(tmp_path / name, sample="sample-shop", with_origin=False)
    if change:
        rel, old, new = change
        text = (repo / rel).read_text()
        assert text.count(old) == 1, (rel, old)
        (repo / rel).write_text(text.replace(old, new))
    return repo


@pytest.mark.parametrize("issue", [7, 8, 9])
def test_shop_acceptance_tests_tell_right_fixes_from_wrong_ones(tmp_path: Path, issue: int) -> None:
    sandbox = LocalPytestRunner()
    task = TASKS[f"shop-{issue}"]
    results = {
        name: run_acceptance(task, shop_variant(tmp_path, name, SHOP_FIXES[issue].get(name)), sandbox)["status"]
        for name in ("as shipped", "right", "wrong")
    }
    assert results == {"as shipped": "FAILED", "right": "PASSED", "wrong": "FAILED"}


def test_the_shop_sample_passes_its_own_tests_as_shipped_and_after_every_right_fix(tmp_path: Path) -> None:
    # The bugs hide behind green tests, as in real projects: the agent has to find them from the issue.
    sandbox = LocalPytestRunner()
    for issue in (None, 7, 8, 9):
        repo = shop_variant(tmp_path, f"own-{issue}", SHOP_FIXES[issue]["right"] if issue else None)
        result = sandbox.run(repo, ["pytest", "-q", "-p", "no:cacheprovider"])
        assert result.exit_code == 0, (issue, result.stdout[-500:])


def test_shop_tasks_use_their_own_sample_and_issues(base: Settings, workspaces: Path) -> None:
    settings = task_settings(base, TASKS["shop-8"], workspaces)
    assert settings.repo_path == workspaces / "sample-shop" and settings.writable_paths == ("shop", "tests")
    assert settings.github_issues_file.name == "sample-shop.json" and settings.test_command.startswith("pytest")
    check_allowed(settings, workspaces)
    done = prepare_trial(8, settings.repo_path, settings.data_dir, language="python", sample="sample-shop")
    assert (settings.repo_path / "shop" / "checkout.py").is_file() and done["starting_commits"] == []
