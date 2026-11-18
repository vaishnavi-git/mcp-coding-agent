"""Benchmark mode: run the sample tasks with an automatic reviewer and hidden acceptance tests.

Comparing models or per-step settings needs more runs than a person can review, so this answers
the checkpoints itself with a fixed policy and then judges each result with tests the agent never
saw. It exists for evaluation only:

- it refuses to run unless GITHUB_MODE=fixture and the repository is one of the bundled samples
  under workspaces/, so it can never approve its own changes to a real repository;
- every decision it makes is recorded as ``decided_by: evaluator``, never as a person;
- the API and the UI never use it. The agent itself always waits for a human.

The policy: approve the plan and the patches, allow one more attempt at the first recovery
checkpoint and stop at the second, and publish up to a pull request (in fixture mode that is a
local bare repository and a JSON file). Then the task's acceptance test is copied next to the
final code and run in that language's Docker sandbox.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.runner import AgentRunner
from app.config import PROJECT_ROOT, Settings
from app.evaluation.trials import prepare_trial
from app.languages import PROFILES, SAMPLES, runner_for
from app.mcp.sandbox_server import DockerRunner, SandboxRunner, copy_repository, format_result
from app.reliability.test_results import parse_output

EVALUATOR = "evaluator"
TASKS_FILE = PROJECT_ROOT / "examples" / "eval" / "tasks.json"
ACCEPTANCE_DIR = PROJECT_ROOT / "examples" / "eval" / "acceptance"
MAX_DECISIONS = 20  # far more checkpoints than any run should reach


class BenchmarkRefused(Exception):
    """The benchmark would run somewhere it must not."""


@dataclass(frozen=True)
class Task:
    id: str
    language: str
    issue: int
    acceptance: Path  # the hidden test file
    install_as: str  # where it goes in the repository copy
    command: str  # how to run it
    sample: str | None = None  # a bundled sample other than the language's default


def load_tasks(path: Path = TASKS_FILE, only: list[str] | None = None) -> list[Task]:
    data = json.loads(Path(path).read_text())
    tasks = [
        Task(t["id"], t["language"], int(t["issue"]), Path(path).parent / "acceptance" / t["acceptance"], t["install_as"], t["command"], t.get("sample"))
        for t in data["tasks"]
    ]
    if only:
        unknown = sorted(set(only) - {t.id for t in tasks})
        if unknown:
            raise ValueError(f"unknown task(s): {', '.join(unknown)} (known: {', '.join(t.id for t in tasks)})")
        tasks = [t for t in tasks if t.id in only]
    return tasks


def task_settings(base: Settings, task: Task, workspaces: Path = PROJECT_ROOT / "workspaces") -> Settings:
    """Settings for one task: its sample, the language's test command and image, and the fixture issues."""
    profile = PROFILES[task.language]
    sample = SAMPLES[task.sample or profile.sample]
    return dataclasses.replace(
        base,
        repo_path=Path(workspaces) / sample.name,
        project_language=task.language,
        writable_paths=sample.writable_paths,
        github_repository=f"demo/{sample.name}",
        github_issues_file=PROJECT_ROOT / "examples" / "issues" / sample.issues_file,
        test_command=profile.test_command,
        test_command_allowlist=profile.test_command_allowlist,
        sandbox_image=profile.sandbox_image,
        require_plan_approval=False,
    )


def check_allowed(settings: Settings, workspaces: Path = PROJECT_ROOT / "workspaces") -> None:
    """Raise BenchmarkRefused unless this is fixture mode on a bundled sample under ``workspaces``."""
    if settings.github_mode != "fixture":
        raise BenchmarkRefused(
            f"the benchmark approves its own pull requests, so it only runs with GITHUB_MODE=fixture (this is {settings.github_mode!r})"
        )
    allowed = {(Path(workspaces) / name).resolve() for name in SAMPLES}
    if Path(settings.repo_path).resolve() not in allowed:
        names = ", ".join(sorted(f"workspaces/{name}" for name in SAMPLES))
        raise BenchmarkRefused(f"the benchmark only runs on the bundled sample repositories ({names}), not {settings.repo_path}")


def evaluator_decision(pending: dict[str, Any], recoveries: int) -> tuple[bool, dict[str, Any]]:
    """The fixed review policy: (approved, extra arguments for resume)."""
    kind = pending.get("kind")
    if kind in ("plan_approval", "patch_approval"):
        return True, {}
    if kind == "recovery":
        return recoveries == 0, {}  # one more attempt, then stop
    if kind == "publish_approval":
        return True, {"publish_up_to": "pull_request"}
    raise BenchmarkRefused(f"unknown checkpoint kind {kind!r}")


def run_acceptance(task: Task, repo: Path, sandbox: SandboxRunner) -> dict[str, Any]:
    """Copy the hidden test next to the final code and run it in the sandbox."""
    argv = shlex.split(task.command)
    with tempfile.TemporaryDirectory(prefix="acceptance-") as tmp:
        copy = Path(tmp) / "repo"
        copy_repository(repo, copy)
        target = copy / task.install_as
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task.acceptance, target)
        result = sandbox.run(copy, argv)
    text = format_result(result, sandbox.name, getattr(sandbox, "timeout_seconds", 0))
    status = text.splitlines()[0].removeprefix("status: ")
    parsed = parse_output(result.stdout, runner_for(argv))
    return {"status": status, "summary": parsed["summary"], "failing_tests": parsed["failing"], "command": task.command}


async def run_task(
    task: Task,
    settings: Settings,
    *,
    runner: AgentRunner | None = None,
    sandbox: SandboxRunner | None = None,
    workspaces: Path = PROJECT_ROOT / "workspaces",
) -> dict[str, Any]:
    """One benchmark run of ``task``: prepare the sample, run the agent with the evaluator, judge the result."""
    check_allowed(settings, workspaces)
    prepare_trial(task.issue, settings.repo_path, settings.data_dir, language=task.language, sample=task.sample)
    runner = runner or AgentRunner(settings)
    view = await runner.start(task.issue)
    run_id = view["run"]["id"]
    runner.audit.log_event(run_id, "benchmark", {"task": task.id, "reviewer": EVALUATOR})
    recoveries = 0
    for _ in range(MAX_DECISIONS):
        if view["run"]["status"] != "awaiting_approval":
            break
        pending = view["pending_approval"] or {}
        approved, extra = evaluator_decision(pending, recoveries)
        if pending.get("kind") == "recovery":
            recoveries += 1
        view = await runner.resume(run_id, approved, decided_by=EVALUATOR, **extra)
    if view["state"].get("tests_passed"):
        sandbox = sandbox or DockerRunner(settings.sandbox_image, settings.sandbox_timeout_seconds, settings.sandbox_memory_mb, settings.sandbox_cpus, settings.sandbox_pids, work_dir=settings.sandbox_work_dir)
        acceptance = run_acceptance(task, settings.repo_path, sandbox)
    else:
        acceptance = {"status": "NOT RUN", "summary": "the agent's own tests did not pass", "failing_tests": [], "command": task.command}
    runner.audit.log_event(run_id, "acceptance_tests", {"task": task.id, **acceptance})
    usage = view.get("usage") or {}
    return {"task": task.id, "run_id": run_id, "status": view["run"]["status"], "cost_usd": usage.get("cost_usd"), "acceptance": acceptance}


async def run_benchmark(
    base: Settings,
    tasks: list[Task],
    runs: int = 1,
    *,
    max_total_cost: float | None = None,
    report: Callable[[dict[str, Any]], None] | None = None,
    workspaces: Path = PROJECT_ROOT / "workspaces",
    make_runner: Callable[[Settings], AgentRunner] | None = None,
    sandbox: SandboxRunner | None = None,
) -> dict[str, Any]:
    """Run every task ``runs`` times. Stops before a run once ``max_total_cost`` has been spent."""
    if base.github_mode != "fixture":
        check_allowed(base, workspaces)  # raises: refuse before anything is touched
    results: list[dict[str, Any]] = []
    spent = 0.0
    stopped = None
    for _ in range(runs):
        for task in tasks:
            if max_total_cost is not None and spent >= max_total_cost:
                stopped = f"stopped after spending ${spent:.4f} of the ${max_total_cost:.2f} limit"
                break
            settings = task_settings(base, task, workspaces)
            runner = make_runner(settings) if make_runner else None
            result = await run_task(task, settings, runner=runner, sandbox=sandbox, workspaces=workspaces)
            spent += result["cost_usd"] or 0.0
            results.append(result)
            if report:
                report(result)
        if stopped:
            break
    return {"setup": base.model_setup(), "runs": results, "total_cost_usd": round(spent, 6), "stopped": stopped}
