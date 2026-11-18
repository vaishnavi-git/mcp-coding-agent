"""The Streamlit UI, driven with Streamlit's AppTest against the real API, workflow and MCP servers.

The UI talks to the API over HTTP with urllib. Here those requests go into the FastAPI app through
a TestClient, so every click exercises the real endpoints and workflow. As in the other workflow
tests, the model is scripted and the tests run on this machine instead of in Docker.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.agents.runner import AgentRunner
from app.api import main as api
from app.config import PROJECT_ROOT
from app.llm.base import LLMAuthenticationError
from app.storage.audit import AuditStore
from tests.fakes import LocalPytestRunner, ScriptedProvider, as_configured, inprocess_hub_factory
from tests.test_agent_workflow import FULL_FIX, INVESTIGATE, PLAN, REGRESSION_TEST

UI = str(PROJECT_ROOT / "app" / "ui" / "streamlit_app.py")


def scripted() -> ScriptedProvider:
    return ScriptedProvider(
        plan=PLAN,
        turns={
            "investigate": list(INVESTIGATE),
            "propose": [[("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX}), ("propose_patch", {"path": "tests/test_sessions.py", "patch": REGRESSION_TEST})], "Fix rotation."],
        },
        summary="Rotated refresh tokens and published the fix.",
    )


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def provider() -> ScriptedProvider:
    return scripted()


@pytest.fixture
def app_test(settings, provider, monkeypatch):
    """An AppTest of the UI whose HTTP calls reach the real API app."""
    audit = AuditStore(settings.database_path)
    runner = AgentRunner(settings, audit=audit, provider_factory=lambda run_id: as_configured(provider, settings), hub_factory=inprocess_hub_factory(settings, LocalPytestRunner(), audit))
    api.app.dependency_overrides[api.get_runner] = lambda: runner
    api._runner = runner
    requests: list[tuple[str, str]] = []
    with TestClient(api.app) as client:

        def urlopen(req: urllib.request.Request, timeout: float | None = None) -> _Response:
            path = "/" + req.full_url.split("://", 1)[1].split("/", 1)[1]
            requests.append((req.get_method(), path))
            response = client.request(req.get_method(), path, json=json.loads(req.data) if req.data else None)
            if response.status_code >= 400:
                raise urllib.error.HTTPError(req.full_url, response.status_code, "error", hdrs=None, fp=io.BytesIO(response.content))  # type: ignore[arg-type]
            return _Response(response.content)

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        monkeypatch.setenv("AGENT_API_URL", "http://agent.test")
        at = AppTest.from_file(UI, default_timeout=120)
        at.requests = requests  # type: ignore[attr-defined]
        yield at
    api.app.dependency_overrides.clear()
    api._runner = None


def texts(at: AppTest) -> str:
    """Everything the page shows as text, for simple checks."""
    parts = [m.value for m in at.markdown] + [s.value for s in at.subheader] + [c.value for c in at.caption]
    parts += [e.value for e in at.error] + [i.value for i in at.info] + [w.value for w in at.warning] + [c.value for c in at.code]
    return "\n".join(str(p) for p in parts)


def button(at: AppTest, label: str):
    return next(b for b in [*at.button, *at.sidebar.button] if b.label.startswith(label))


def test_with_no_runs_the_page_asks_to_start_one(app_test: AppTest) -> None:
    at = app_test.run()
    assert not at.exception
    assert at.title[0].value == "MCP Coding Agent"
    assert [i.value for i in at.info] == ["Start a run from the sidebar."]
    assert at.sidebar.number_input[0].value == 42


def test_a_run_from_start_to_published_pull_request(app_test: AppTest, settings) -> None:
    at = app_test.run()
    button(at, "Start").click().run()
    assert not at.exception
    page = texts(at)
    assert "### Plan" in page and "### Approval needed (initial proposal)" in page
    assert "**authkit/sessions.py** (+2 / -0)" in page and "+        self._by_refresh[new_session.refresh_token] = new_session" in page
    assert ("POST", "/runs") in at.requests  # type: ignore[attr-defined]

    button(at, "Approve and apply").click().run()
    page = texts(at)
    assert "### Applied changes" in page and "### Test runs (Docker sandbox)" in page
    assert "### Approval needed: publish to GitHub" in page
    titles = [t for t in at.text_input if t.label == "Pull request title"]
    assert titles and titles[0].value == "Rotate refresh tokens on refresh"
    titles[0].set_value("Rotate refresh tokens (reviewed)")
    button(at, "Approve and publish").click().run()

    assert not at.exception
    assert any(m.label == "Status" and m.value == "published" for m in at.metric)
    pull_request = json.loads(sorted((settings.data_dir / "pull_requests").glob("*.json"))[0].read_text())
    assert pull_request["title"] == "Rotate refresh tokens (reviewed)"  # the reviewer's edit, made in the UI
    assert "Rotated refresh tokens and published the fix." in texts(at)  # the run summary


def test_rejecting_a_patch_in_the_ui_writes_nothing(app_test: AppTest, settings) -> None:
    at = app_test.run()
    button(at, "Start").click().run()
    before = (settings.repo_path / "authkit" / "sessions.py").read_text()
    button(at, "Reject").click().run()
    assert not at.exception
    assert any(m.label == "Status" and m.value == "rejected" for m in at.metric)
    assert (settings.repo_path / "authkit" / "sessions.py").read_text() == before


def test_a_failed_run_shows_the_code_the_hint_and_a_retry_button(app_test: AppTest, provider: ScriptedProvider) -> None:
    provider.errors["plan_searches"] = LLMAuthenticationError("Anthropic rejected the API key (401). Check ANTHROPIC_API_KEY.", status_code=401)
    at = app_test.run()
    button(at, "Start").click().run()
    assert not at.exception
    assert any("**llm_auth** during `plan`" in e.value for e in at.error)
    assert any(i.value.startswith("What to do:") and "ANTHROPIC_API_KEY" in i.value for i in at.info)
    retry = button(at, "Retry from 'plan'")
    provider.errors.clear()
    retry.click().run()
    assert not at.exception and "### Approval needed (initial proposal)" in texts(at)  # the retry got as far as the patch


def test_an_unreachable_api_is_reported_not_crashed(app_test: AppTest, monkeypatch) -> None:
    def down(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    at = app_test.run()
    assert not at.exception
    assert any(e.value.startswith("Cannot reach the API at http://agent.test") for e in at.error)
