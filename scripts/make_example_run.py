"""Produce examples/runs/issue-42-scripted-run.md: a complete run of issue #42 without an API key.

Everything is real except the model: the LangGraph workflow, the four MCP
servers (in-process), git, the approval checkpoints, and pytest on a copy of
the sample repository. The model's turns are scripted with the test doubles in
tests/fakes.py (a first patch that is incomplete, then the fix), and pytest
runs on the local machine instead of in Docker. Token counts are zero and
costs n/a because no model was called. Everything happens in a temporary
directory; the project's data/ and workspaces/ are not touched.

Usage: python scripts/make_example_run.py [--out examples/runs/issue-42-scripted-run.md]
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.runner import AgentRunner  # noqa: E402
from app.config import PROJECT_ROOT  # noqa: E402
from app.evaluation import load_runs  # noqa: E402
from app.evaluation.run_report import render_run  # noqa: E402
from app.sample_repo import setup_sample_repo  # noqa: E402
from app.storage.audit import AuditStore  # noqa: E402
from tests.conftest import make_settings  # noqa: E402
from tests.fakes import LocalPytestRunner, ScriptedProvider, inprocess_hub_factory  # noqa: E402
from tests.test_agent_workflow import INVESTIGATE, PARTIAL_FIX, PLAN, REGRESSION_TEST, REVOKE_USED_TOKEN  # noqa: E402

NOTE = (
    "Example run, produced by `scripts/make_example_run.py` and rendered by `scripts/render_run.py` on {date}. "
    "Everything except the model is real: the LangGraph workflow, the four MCP servers, git, the approval checkpoints "
    "(answered by the script), and pytest on a copy of the sample repository. The model's turns are **scripted** "
    "(an incomplete first patch, then the fix), so the plan, findings and patches are fixed inputs, not Claude's output. "
    "Token counts are zero and costs n/a because no model was called, and timings reflect a scripted model. "
    "Tests ran with a local pytest stand-in instead of Docker. This is not an evaluation result."
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "examples" / "runs" / "issue-42-scripted-run.md")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="mcp-agent-example-") as tmp:
        repo = setup_sample_repo(Path(tmp) / "sample-repo")
        settings = dataclasses.replace(make_settings(repo, Path(tmp) / "data"), anthropic_model=ScriptedProvider.model, max_task_cost_usd=0)
        provider = ScriptedProvider(
            plan={**PLAN, "files_to_change": ["authkit/sessions.py", "tests/test_sessions.py"]},
            turns={
                "investigate": list(INVESTIGATE),
                "propose": [
                    [("propose_patch", {"path": "authkit/sessions.py", "patch": PARTIAL_FIX})],
                    "Store the new refresh token so the next refresh works.",
                    [("propose_patch", {"path": "authkit/sessions.py", "patch": REVOKE_USED_TOKEN}), ("propose_patch", {"path": "tests/test_sessions.py", "patch": REGRESSION_TEST})],
                    "The used refresh token must be revoked as well; adding a regression test for two refreshes in a row.",
                ],
            },
            summary="Refresh tokens are now rotated: refresh() stores the new refresh token and revokes the used one. "
            "A regression test covers two refreshes in a row; all tests pass.",
            usage=(0, 0),
        )
        audit = AuditStore(settings.database_path)
        runner = AgentRunner(settings, audit=audit, provider_factory=lambda run_id: provider, hub_factory=inprocess_hub_factory(settings, LocalPytestRunner(), audit))
        view = await runner.start(42)
        run_id = view["run"]["id"]
        view = await runner.resume(run_id, approved=True)  # first patch: tests fail, the model proposes a fix
        view = await runner.resume(run_id, approved=True)  # fix and regression test: tests pass
        view = await runner.resume(run_id, approved=True, publish_up_to="pull_request")
        assert view["run"]["status"] == "published", view["run"]
        record = (await load_runs(settings, run_ids=[run_id], runner=runner))[0]
        text = render_run(record, note=NOTE.format(date=datetime.now(UTC).strftime("%Y-%m-%d")))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
