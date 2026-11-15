"""Phase 6: evaluation metrics and run reports, checked against runs with known outcomes.

The runs are real workflow runs (real MCP servers, real approvals, a scripted
model and canned test results), recorded in one audit database. The expected
values below are worked out by hand from what each scenario does.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.evaluation import load_runs, run_metrics, summarize
from app.evaluation.metrics import files_in_diff, percentile
from app.evaluation.report import render_markdown
from app.evaluation.run_report import render_run
from app.sample_repo import setup_sample_repo
from tests.fakes import FakeRunner, ScriptedProvider
from tests.test_agent_workflow import FULL_FIX, PARTIAL_FIX, PLAN, REGRESSION_TEST, REVOKE_USED_TOKEN
from tests.test_reliability_workflow import INVESTIGATE, PASS, ROTATION_TEST, make_runner, pytest_output

pytestmark = pytest.mark.anyio

INIT_COMMENT = '--- a/authkit/__init__.py\n+++ b/authkit/__init__.py\n@@ -1,3 +1,4 @@\n+# Session handling for authkit.\n from .sessions import InvalidToken, Session, SessionStore\n \n __all__ = ["InvalidToken", "Session", "SessionStore"]\n'
INIT_REVERT = '--- a/authkit/__init__.py\n+++ b/authkit/__init__.py\n@@ -1,4 +1,3 @@\n-# Session handling for authkit.\n from .sessions import InvalidToken, Session, SessionStore\n \n __all__ = ["InvalidToken", "Session", "SessionStore"]\n'
FAIL = pytest_output(ROTATION_TEST)


def plan(*files: str) -> dict:
    return {**PLAN, "files_to_change": list(files)}


def proposals(*pairs: tuple[str, str]) -> list[tuple[str, dict]]:
    return [("propose_patch", {"path": path, "patch": patch}) for path, patch in pairs]


def provider(files: tuple[str, ...], *turns) -> ScriptedProvider:
    return ScriptedProvider(plan=plan(*files), turns={"investigate": list(INVESTIGATE), "propose": list(turns)})


async def record_scenarios(settings) -> dict[str, str]:
    """Six runs with known outcomes in one audit database; returns run ids by scenario name."""
    ids: dict[str, str] = {}

    async def fresh(name, scripted, sandbox, **overrides):
        setup_sample_repo(settings.repo_path, force=True)
        runner, _ = make_runner(dataclasses.replace(settings, **overrides), scripted, sandbox)
        view = await runner.start(42)
        ids[name] = view["run"]["id"]
        return runner, view

    # A: fix and regression test, tests pass first time, pull request created.
    runner, _ = await fresh("published", provider(("authkit/sessions.py", "tests/test_sessions.py"),
                                                  proposals(("authkit/sessions.py", FULL_FIX), ("tests/test_sessions.py", REGRESSION_TEST)), "Fix."), FakeRunner([PASS]))
    await runner.resume(ids["published"], approved=True)
    assert (await runner.resume(ids["published"], approved=True))["run"]["status"] == "published"

    # B: partial fix plus an unrelated comment; tests fail; the fix attempt completes the fix and
    # removes the comment again (an unplanned, reverted edit); publication declined.
    runner, view = await fresh("recovered", provider(("authkit/sessions.py",),
                                                  proposals(("authkit/sessions.py", PARTIAL_FIX), ("authkit/__init__.py", INIT_COMMENT)), "Partial.",
                                                  proposals(("authkit/sessions.py", REVOKE_USED_TOKEN), ("authkit/__init__.py", INIT_REVERT)), "Complete."),
                            FakeRunner([FAIL, PASS]))
    assert view["pending_approval"]["outside_plan"] == ["authkit/__init__.py"]  # flagged for the reviewer
    await runner.resume(ids["recovered"], approved=True)  # tests fail -> fix proposed
    await runner.resume(ids["recovered"], approved=True)  # tests pass -> publication checkpoint
    assert (await runner.resume(ids["recovered"], approved=False))["run"]["status"] == "completed"

    # C: tests fail, no fix attempts allowed, the reviewer stops at the recovery checkpoint.
    runner, _ = await fresh("stopped", provider(("authkit/sessions.py",),
                                                proposals(("authkit/sessions.py", FULL_FIX), ("tests/test_sessions.py", REGRESSION_TEST)), "Fix."),
                            FakeRunner([FAIL]), max_fix_attempts=0)
    view = await runner.resume(ids["stopped"], approved=True)
    assert view["pending_approval"]["kind"] == "recovery"
    assert (await runner.resume(ids["stopped"], approved=False))["run"]["status"] == "tests_failed"

    # D: the patch is rejected without feedback.
    runner, _ = await fresh("rejected", provider(("authkit/sessions.py",), proposals(("authkit/sessions.py", FULL_FIX)), "Fix."), FakeRunner())
    assert (await runner.resume(ids["rejected"], approved=False))["run"]["status"] == "rejected"

    # E: the model-call limit stops the run after planning.
    _, view = await fresh("budget", provider(("authkit/sessions.py",)), FakeRunner(), max_llm_calls_per_run=1)
    assert view["failure"]["code"] == "budget_exceeded"

    # F: still waiting at the patch checkpoint.
    _, view = await fresh("waiting", provider(("authkit/sessions.py",), proposals(("authkit/sessions.py", FULL_FIX)), "Fix."), FakeRunner())
    assert view["pending_approval"]["kind"] == "patch_approval"
    return ids


async def test_metrics_match_hand_computed_values(settings) -> None:
    ids = await record_scenarios(settings)
    records = await load_runs(settings)
    assert [r.run["id"] for r in records] == list(ids.values())  # oldest first
    rows = {name: run_metrics(next(r for r in records if r.run["id"] == run_id)) for name, run_id in ids.items()}

    a, b, c, d, e, f = (rows[n] for n in ("published", "recovered", "stopped", "rejected", "budget", "waiting"))
    assert (a["resolved"], b["resolved"], c["resolved"], d["resolved"], e["resolved"], f["resolved"]) == (True, True, False, False, False, False)
    assert (a["published"], b["published"]) == (True, False)
    assert (a["test_runs"], b["test_runs"], c["test_runs"], d["test_runs"]) == (1, 2, 1, 0)
    assert (a["first_test_run_passed"], b["first_test_run_passed"], c["first_test_run_passed"], d["first_test_run_passed"]) == (True, False, False, None)
    assert (b["had_test_failure"], b["recovered"], c["had_test_failure"], c["recovered"]) == (True, True, True, False)
    assert b["files_edited"] == ["authkit/sessions.py", "authkit/__init__.py"]
    assert b["unplanned_edits"] == ["authkit/__init__.py"] and b["reverted_edits"] == ["authkit/__init__.py"]
    assert c["unplanned_edits"] == ["tests/test_sessions.py"] and c["reverted_edits"] == []
    assert a["unplanned_edits"] == [] and d["files_edited"] == [] and e["files_edited"] == []
    assert e["failure_code"] == "budget_exceeded" and e["llm_calls"] == 1
    assert not f["finished"] and f["wall_seconds"] is None
    assert a["tool_calls_by_model"] == 4 and a["tool_calls"] == a["tool_calls_by_model"] + a["tool_calls_by_workflow"]
    assert all(r["cost_usd"] == pytest.approx(0.004 * r["llm_calls"]) for r in rows.values())  # $2/$10 per MTok, 1000 in / 200 out

    overall = summarize(list(rows.values()))["overall"]
    assert (overall["runs"], overall["finished"], overall["unfinished"], overall["resolved"]) == (6, 5, 1, 2)
    assert overall["resolution_rate"] == pytest.approx(2 / 5)
    assert overall["published_rate"] == pytest.approx(1 / 5)
    assert overall["first_attempt_pass_rate"] == pytest.approx(1 / 3)  # A of A, B, C
    assert overall["test_run_pass_rate"] == pytest.approx(2 / 4)  # A:1/1, B:1/2, C:0/1
    assert overall["recovery_rate"] == pytest.approx(1 / 2) and overall["runs_with_test_failure"] == 2
    assert overall["unplanned_edits"] == 2 and overall["unplanned_edit_rate"] == pytest.approx(2 / 6)
    assert overall["reverted_edits"] == 1 and overall["reverted_edit_rate"] == pytest.approx(1 / 6)
    finished = [rows[n] for n in ("published", "recovered", "stopped", "rejected", "budget")]
    total = sum(r["cost_usd"] for r in finished)
    assert overall["total_cost_usd"] == pytest.approx(total)
    assert overall["cost_per_resolved_task_usd"] == pytest.approx(total / 2)
    assert overall["mean_cost_per_run_usd"] == pytest.approx(total / 5)
    assert overall["mean_tool_calls"] == pytest.approx(sum(r["tool_calls"] for r in finished) / 5)
    assert overall["status_counts"] == {"awaiting_approval": 1, "completed": 1, "failed": 1, "published": 1, "rejected": 1, "tests_failed": 1}
    assert set(overall["step_duration_ms"]) >= {"fetch_issue", "plan", "investigate", "propose", "apply", "run_tests"}
    assert overall["mean_human_wait_seconds"] >= 0 and overall["mean_agent_seconds"] > 0

    text = render_markdown(summarize(list(rows.values())), list(rows.values()), source="test.db", selection="all recorded runs")
    assert "| Issue resolution rate | 40.0% (2 of 5) |" in text
    assert "| Recovery rate after test failures | 50.0% (of 2 runs with a failure) |" in text
    assert "| Unplanned file edits | 2 (33.3% of edited files) |" in text
    assert f"| `{ids['budget']}` | #42 | `claude-sonnet-5` | failed (budget_exceeded) | no |" in text


async def test_unknown_prices_and_empty_selections_give_na_not_numbers(settings) -> None:
    unpriced = dataclasses.replace(settings, anthropic_model="unpriced-model", max_task_cost_usd=0)
    runner, _ = make_runner(unpriced, provider(("authkit/sessions.py",), proposals(("authkit/sessions.py", FULL_FIX)), "Fix."), FakeRunner([PASS]))
    run_id = (await runner.start(42))["run"]["id"]
    await runner.resume(run_id, approved=True)
    await runner.resume(run_id, approved=True)
    rows = [run_metrics(r) for r in await load_runs(settings)]
    overall = summarize(rows)["overall"]
    assert rows[0]["resolved"] and rows[0]["cost_usd"] is None
    assert overall["total_cost_usd"] is None and overall["cost_per_resolved_task_usd"] is None
    assert overall["recovery_rate"] is None  # no test run failed, so there is nothing to recover from
    text = render_markdown(summarize(rows), rows, source="test.db", selection="all")
    assert "| Cost per resolved task | n/a |" in text and "| Recovery rate after test failures | n/a (of 0 runs with a failure) |" in text
    assert "nothing was measured" in render_markdown(summarize([]), [], source="test.db", selection="none")
    assert await load_runs(settings, issues=[999]) == [] and await load_runs(settings, run_ids=["nope"]) == []


async def test_run_report_walks_through_the_run(settings) -> None:
    setup_sample_repo(settings.repo_path, force=True)
    scripted = provider(("authkit/sessions.py", "tests/test_sessions.py"), proposals(("authkit/sessions.py", FULL_FIX), ("tests/test_sessions.py", REGRESSION_TEST)), "Fix.")
    runner, _ = make_runner(settings, scripted, FakeRunner([PASS]))
    run_id = (await runner.start(42))["run"]["id"]
    await runner.resume(run_id, approved=True)
    await runner.resume(run_id, approved=True)
    text = render_run((await load_runs(settings, run_ids=[run_id]))[0], note="Scripted model, test double for Docker.")
    for expected in (f"# Run `{run_id}`: issue #42", "> Scripted model, test double for Docker.", "## 1. Issue", "## 2. Plan",
                     "Files expected to change: `authkit/sessions.py`, `tests/test_sessions.py`", "## 3. Investigation",
                     "## 5. Applied changes", "+        self._by_refresh.pop(refresh_token, None)", "## 6. Test runs", "| 0 | PASSED |",
                     "## 7. Publication", "- pull request: fixture://demo/sample-repo/pull/1 (draft) into `main`", "Fixes #42",
                     "## 9. Tool calls (audit log)", "`apply_patch` | write", "## 10. Model calls"):
        assert expected in text, expected


def test_percentiles_and_diff_paths() -> None:
    assert percentile([], 50) is None
    assert percentile(range(1, 11), 50) == 5 and percentile(range(1, 11), 95) == 10 and percentile([7], 95) == 7
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n(new protected file not shown: .env)\n(new symbolic link not shown: tests/link)\n"
    assert files_in_diff(diff) == {"x.py", ".env", "tests/link"} and files_in_diff(None) == set()


async def test_scripts_read_the_recorded_runs(settings, tmp_path: Path) -> None:
    await record_scenarios(settings)
    env = {**os.environ, "DATA_DIR": str(settings.data_dir), "PYTHONPATH": str(PROJECT_ROOT)}

    def script(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *args], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120)

    md = script("scripts/evaluate.py", "--issue", "42")
    assert md.returncode == 0, md.stderr
    assert "Selection: issues #42." in md.stdout and "| Runs (finished / still open) | 5 / 1 |" in md.stdout
    assert "from `DATA_DIR/agent.db`" in md.stdout and str(tmp_path) not in md.stdout  # no absolute paths in reports
    as_json = json.loads(script("scripts/evaluate.py", "--json").stdout)
    assert as_json["summary"]["overall"]["resolved"] == 2 and len(as_json["runs"]) == 6
    out = tmp_path / "results.md"
    assert script("scripts/evaluate.py", "--since", "2000-01-01", "--out", str(out)).returncode == 0 and out.read_text().startswith("# Evaluation results")
    later = script("scripts/evaluate.py", "--since", "2999-01-01")
    assert "No runs matched the selection" in later.stdout
    report = script("scripts/render_run.py", "--latest")
    assert report.returncode == 0 and report.stdout.startswith("# Run `") and "Waiting at checkpoint:** `patch_approval`" in report.stdout

    empty = {**env, "DATA_DIR": str(tmp_path / "no-data")}
    missing = subprocess.run([sys.executable, "scripts/evaluate.py"], cwd=PROJECT_ROOT, env=empty, capture_output=True, text=True, timeout=60)
    assert missing.returncode == 1 and "no runs have been recorded" in missing.stderr
    assert not (tmp_path / "no-data").exists()  # evaluating never creates a database


def test_trials_start_from_a_clean_and_fair_state(tmp_path: Path) -> None:
    from app.evaluation.trials import prepare_trial
    from app.mcp.git_utils import run_git
    from tests.fakes import LocalPytestRunner

    repo, data = tmp_path / "sample-repo", tmp_path / "data"
    (data / "pull_requests").mkdir(parents=True)
    (data / "pull_requests" / "1.json").write_text('{"head": "agent/issue-42-x", "state": "open"}')

    done = prepare_trial(42, repo, data)
    assert done["starting_commits"] == [] and done["archived_pull_requests"]
    assert not (data / "pull_requests").exists() and len(list((data / "pull_requests-archive").rglob("1.json"))) == 1
    assert LocalPytestRunner().run(repo, ["pytest", "-q", "-p", "no:cacheprovider"]).exit_code == 1  # issue 42's bug is there to fix

    done = prepare_trial(43, repo, data)
    assert done["starting_commits"] == ["Fix refresh token rotation (issue 42): starting point for issue 43 trials"]
    result = LocalPytestRunner().run(repo, ["pytest", "-q", "-p", "no:cacheprovider"])
    assert result.exit_code == 0 and "5 passed" in result.stdout  # issue 43 starts green
    assert run_git(repo, "status", "--porcelain").strip() == ""
    assert run_git(repo, "rev-parse", "HEAD").strip() == run_git(repo, "ls-remote", "origin", "refs/heads/main").split()[0]
