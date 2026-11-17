from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings
from app.sample_repo import setup_sample_repo
from tests.spec_matrix import SPEC_CATEGORIES, marker_index

# ---------------------------------------------------------------------- spec categories
_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 2}
_CATEGORIES_BY_NODE = pytest.StashKey[dict[str, set[str]]]()


def pytest_configure(config: pytest.Config) -> None:
    for marker, title in SPEC_CATEGORIES.items():
        config.addinivalue_line("markers", f"{marker}: spec test category '{title}' (see tests/spec_matrix.py)")


@pytest.hookimpl(tryfirst=True)  # before -m selection, so `pytest -m path_traversal` works
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    index = marker_index()
    by_node = config.stash.setdefault(_CATEGORIES_BY_NODE, {})
    for item in items:
        name = getattr(item, "originalname", item.name)
        markers = index.get(f"{item.path.name}::{name}", set())
        for marker in sorted(markers):
            item.add_marker(marker)
        if markers:
            by_node[item.nodeid] = markers


def pytest_terminal_summary(terminalreporter, exitstatus: int, config: pytest.Config) -> None:
    """One line per spec category: how many of its tests passed, failed or were skipped."""
    by_node = config.stash.get(_CATEGORIES_BY_NODE, {})
    outcomes: dict[str, tuple[str, set[str]]] = {}
    for kind in _RANK:
        for report in terminalreporter.stats.get(kind, []):
            keywords = by_node.get(getattr(report, "nodeid", ""), set())
            if not keywords or (kind == "passed" and report.when != "call"):
                continue
            previous = outcomes.get(report.nodeid)
            if previous is None or _RANK[kind] > _RANK[previous[0]]:
                outcomes[report.nodeid] = (kind, keywords)
    if not outcomes:
        return
    terminalreporter.write_sep("-", "spec test categories (tests/spec_matrix.py)")
    for marker, title in SPEC_CATEGORIES.items():
        counts = {kind: 0 for kind in ("passed", "failed", "skipped")}
        for kind, keywords in outcomes.values():
            if marker in keywords:
                counts["failed" if kind == "error" else kind] += 1
        if any(counts.values()):
            line = f"{title:<30} {counts['passed']:>4} passed"
            line += "".join(f", {n} {kind}" for kind, n in counts.items() if kind != "passed" and n)
            terminalreporter.write_line(line, red=bool(counts["failed"]), green=not counts["failed"])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    return setup_sample_repo(tmp_path / "repo")


def make_settings(repo: Path, data_dir: Path, **overrides) -> Settings:
    base = Settings(
        anthropic_api_key=None,
        anthropic_model="claude-sonnet-5",
        anthropic_max_tokens=4096,
        anthropic_effort=None,
        anthropic_thinking="adaptive",
        anthropic_timeout_seconds=30,
        anthropic_max_retries=0,
        repo_path=repo,
        writable_paths=("authkit", "tests"),
        allow_new_files=True,
        github_mode="fixture",
        github_repository="demo/sample-repo",
        github_token=None,
        github_issues_file=PROJECT_ROOT / "examples" / "issues" / "sample-repo.json",
        github_api_url="https://api.github.com",
        github_default_branch="main",
        github_branch_prefix="agent/",
        github_pr_draft=True,
        github_web_url="https://github.com",
        git_author_name="MCP Coding Agent",
        git_author_email="agent@example.invalid",
        sandbox_image="mcp-agent-sandbox:latest",
        sandbox_timeout_seconds=60,
        sandbox_memory_mb=512,
        sandbox_cpus=1.0,
        sandbox_pids=128,
        sandbox_work_dir=None,
        test_command="pytest -q -p no:cacheprovider",
        test_command_allowlist=("pytest", "python -m pytest"),
        docs_enabled=True,
        docs_sources_file=PROJECT_ROOT / "config" / "docs_sources.json",
        docs_timeout_seconds=5.0,
        docs_max_page_bytes=4000,
        docs_max_fetches=40,
        docs_respect_robots=True,
        docs_cache_ttl_seconds=3600,
        max_tool_calls_per_step=10,
        max_fix_attempts=2,
        max_revisions=2,
        max_tool_calls_per_run=150,
        max_llm_calls_per_run=100,
        max_task_cost_usd=5.0,
        model_prices_file=PROJECT_ROOT / "config" / "model_prices.json",
        llm_price_input_per_mtok=None,
        llm_price_output_per_mtok=None,
        llm_price_cache_read_per_mtok=None,
        llm_price_cache_write_per_mtok=None,
        tool_timeout_seconds=30.0,
        tool_max_retries=2,
        retry_base_delay_seconds=0.0,  # tests do not wait between retries
        retry_max_delay_seconds=30.0,
        max_run_retries=3,
        require_plan_approval=False,
        data_dir=data_dir,
    )
    return dataclasses.replace(base, **overrides)


@pytest.fixture
def settings(sample_repo: Path, tmp_path: Path) -> Settings:
    return make_settings(sample_repo, tmp_path / "data")
