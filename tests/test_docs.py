"""The documentation stays true to the code.

Each check compares a document with the code it describes: settings, routes,
the OpenAPI schema, the tool table, the workflow diagram, links and anchors,
and the tests that docs/SECURITY.md cites. A change to the code that is not
reflected in the docs fails here.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.agents.coding_agent import build_graph
from app.api.main import app
from app.config import PROJECT_ROOT
from tests.test_tool_contracts import CATALOG

DOCS = PROJECT_ROOT / "docs"
MARKDOWN = sorted([PROJECT_ROOT / "README.md", PROJECT_ROOT / "CHANGELOG.md", *DOCS.rglob("*.md"), *(PROJECT_ROOT / "examples").rglob("*.md")])


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def settings_read_by_the_code() -> set[str]:
    names = set(re.findall(r'\be\("([A-Z][A-Z0-9_]+)"', read(PROJECT_ROOT / "app" / "config.py")))
    names |= set(re.findall(r'os\.environ\.get\("([A-Z][A-Z0-9_]+)"', read(PROJECT_ROOT / "app" / "ui" / "streamlit_app.py")))
    return names


def test_every_setting_is_in_env_example_and_the_setup_guide() -> None:
    names = settings_read_by_the_code()
    assert len(names) > 50 and "ANTHROPIC_MODEL" in names and "AGENT_API_URL" in names
    env_example, setup = read(PROJECT_ROOT / ".env.example"), read(DOCS / "SETUP.md")
    assert sorted(n for n in names if not re.search(rf"^#?\s*{n}=", env_example, re.MULTILINE)) == []
    assert sorted(n for n in names if f"`{n}`" not in setup) == []


def test_every_route_is_in_the_api_docs() -> None:
    api_doc = read(DOCS / "API.md")
    routes = [(method, route.path) for route in app.routes if isinstance(route, APIRoute) for method in sorted(route.methods)]
    assert len(routes) == 8
    assert [r for r in routes if f"| `{r[0]}` | `{r[1]}` |" not in api_doc] == []


def test_openapi_file_is_current() -> None:
    from scripts.export_openapi import schema_text

    assert read(DOCS / "openapi.json") == schema_text(), "run: python scripts/export_openapi.py"


def test_readme_tool_table_matches_the_discovered_catalog() -> None:
    rows = re.findall(r"^\| (\w+) \| `(\w+)\(([^)]*)\)` \| (\w+) \|", read(PROJECT_ROOT / "README.md"), re.MULTILINE)
    table = {name: (server, risk) for server, name, _, risk in rows}
    assert table == {name: (server, risk.value) for name, (server, risk, _) in CATALOG.items()}


@pytest.mark.anyio
async def test_readme_tool_arguments_match_the_schemas(settings) -> None:
    from app.mcp.client import MCPHub
    from tests.fakes import FakeRunner, inprocess_servers

    rows = re.findall(r"^\| \w+ \| `(\w+)\(([^)]*)\)` \|", read(PROJECT_ROOT / "README.md"), re.MULTILINE)
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        for name, args in rows:
            documented = [a.strip() for a in args.split(",") if a.strip()]
            assert documented == list(hub.get(name).input_schema.get("properties", {})), name


def test_workflow_diagram_matches_the_graph() -> None:
    text = read(DOCS / "ARCHITECTURE.md")
    block = text.split("## Workflow", 1)[1].split("```mermaid", 1)[1].split("```", 1)[0]
    alias = {"START": "__start__", "END": "__end__"}
    documented = {
        (alias.get(a, a), alias.get(b, b))
        for a, b in re.findall(r"^\s*(\w+)(?:\(\[[^\]]*\]\))?\s*-->(?:\|[^|]*\|)?\s*(\w+)", block, re.MULTILINE)
    }
    graph = build_graph(None).compile().get_graph()
    real = {(e.source, e.target) for e in graph.edges}
    # Failure exits (every step -> finalize) are described in the text instead of drawn.
    expected = {(s, t) for s, t in real if t != "finalize" or s == "publish"}
    assert documented == expected


def slug(heading: str) -> str:
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {slug(h) for h in re.findall(r"^#{1,6} (.+)$", read(path), re.MULTILINE)}


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: str(p.relative_to(PROJECT_ROOT)))
def test_relative_links_and_anchors_resolve(path: Path) -> None:
    text = re.sub(r"```.*?```", "", read(path), flags=re.DOTALL)  # links inside code blocks are examples
    broken = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"^[a-z]+://|^mailto:", target):
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / file_part).resolve() if file_part else path
        if not resolved.exists() or anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
            broken.append(target)
    assert broken == []


def test_security_doc_cites_tests_that_exist() -> None:
    refs = sorted(set(re.findall(r"tests/(test_\w+)\.py::(test_\w+)", read(DOCS / "SECURITY.md"))))
    assert len(refs) > 50
    missing = [f"{module}::{name}" for module, name in refs if not inspect.isfunction(getattr(importlib.import_module(f"tests.{module}"), name, None))]
    assert missing == []


def test_no_model_name_or_key_in_python_code() -> None:
    """'Do not hardcode the API key or model name inside Python files.'"""
    model = re.compile(r"\bclaude-(?:opus|sonnet|haiku|fable|mythos|instant|\d)[\w.-]*", re.IGNORECASE)
    key = re.compile(r"sk-ant-(?:api|admin)\d+-[A-Za-z0-9_-]{20,}")
    offenders = []
    for path in sorted([*(PROJECT_ROOT / "app").rglob("*.py"), *(PROJECT_ROOT / "scripts").rglob("*.py")]):
        text = read(path)
        offenders += [f"{path.relative_to(PROJECT_ROOT)}: {m.group(0)}" for m in [*model.finditer(text), *key.finditer(text)]]
    assert offenders == []
    # The model is configured in .env.example and the price file instead.
    assert re.search(r"^ANTHROPIC_MODEL=claude-", read(PROJECT_ROOT / ".env.example"), re.MULTILINE)
    assert re.search(r"^ANTHROPIC_API_KEY=your_anthropic_api_key$", read(PROJECT_ROOT / ".env.example"), re.MULTILINE)


def test_docs_have_no_placeholders_or_invented_results() -> None:
    for path in MARKDOWN:
        text = read(path)
        assert "PLACEHOLDER" not in text and "TODO" not in text, path
    # Until generated reports exist, the evaluation docs say there are no results.
    reports = [p for p in (DOCS / "evaluation").glob("results-*") if p.is_file()]
    if not reports:
        assert "No experiments have been run yet" in read(DOCS / "EVALUATION.md")
        assert "None yet" in read(PROJECT_ROOT / "README.md").split("## Results", 1)[1].split("##", 1)[0]


def test_example_run_says_how_it_was_produced() -> None:
    text = read(PROJECT_ROOT / "examples" / "runs" / "issue-42-scripted-run.md")
    note = text.split("\n\n", 2)[1]
    assert note.startswith("> Example run") and "scripted" in note and "not an evaluation result" in note
    assert "| published | `scripted-model` |" in text and "## 6. Test runs" in text
    assert "/Users/" not in text and "/home/" not in text and "/tmp/" not in text  # no local paths


def test_published_reports_contain_no_local_paths_or_keys() -> None:
    generated = [*(DOCS / "evaluation").glob("results-*"), *(PROJECT_ROOT / "examples" / "runs").glob("*.md")]
    assert generated
    for path in generated:
        text = read(path)
        assert not re.search(r"/Users/|/home/|/private/var/|/var/folders/|sk-ant-|ghp_[A-Za-z0-9]{20}", text), path


def test_issue_fixtures_are_described() -> None:
    described = read(PROJECT_ROOT / "examples" / "issues" / "README.md")
    for name, numbers in (("sample-repo.json", [42, 43]), ("sample-repo-js.json", [42, 43]), ("sample-repo-java.json", [42, 43]), ("sample-shop.json", [7, 8, 9])):
        issues = json.loads(read(PROJECT_ROOT / "examples" / "issues" / name))["issues"]
        assert [issue["number"] for issue in issues] == numbers, name
        assert all(f"## #{issue['number']}: {issue['title']}" in described for issue in issues), name
        assert name in described
