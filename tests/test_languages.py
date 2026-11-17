"""JavaScript and Java support: language detection, settings, reading test output, the samples,
a JavaScript run through the whole workflow, and the two sandbox images in real Docker.

The runner output in tests/data/runner_output/ was recorded from the real sandbox images
(``docker build`` of sandbox/javascript/ and sandbox/java/) running the sample repositories.
"""

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings
from app.languages import PROFILES, detect_language, resolve_language, runner_for
from app.mcp.sandbox_server import DockerRunner, ExecResult, format_result
from app.reliability.test_results import analyze, parse_output
from app.sample_repo import setup_sample_repo
from tests.conftest import make_settings
from tests.fakes import LocalPytestRunner, ScriptedProvider
from tests.test_agent_workflow import make_runner

pytestmark = pytest.mark.anyio

OUTPUT = Path(__file__).parent / "data" / "runner_output"


def recorded(name: str) -> tuple[int, str]:
    first, _, rest = (OUTPUT / name).read_text().partition("\n")
    return int(first.split()[3].rstrip(";")), rest


# --------------------------------------------------------------------------- detection and settings


@pytest.mark.parametrize(
    "marker, language",
    [("pyproject.toml", "python"), ("requirements.txt", "python"), ("setup.py", "python"), ("package.json", "javascript"), ("pom.xml", "java")],
)
def test_language_is_detected_from_marker_files(tmp_path: Path, marker: str, language: str) -> None:
    (tmp_path / marker).write_text("")
    assert detect_language(tmp_path) == language
    assert resolve_language("auto", tmp_path) == language


def test_detection_order_and_fallback(tmp_path: Path) -> None:
    assert detect_language(tmp_path) is None
    assert resolve_language(None, tmp_path) == "python"  # nothing to go on
    (tmp_path / "pom.xml").write_text("")
    (tmp_path / "package.json").write_text("{}")
    assert detect_language(tmp_path) == "javascript"  # package.json is checked before pom.xml
    (tmp_path / "pyproject.toml").write_text("")
    assert detect_language(tmp_path) == "python"


def test_explicit_language_aliases_and_errors(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    assert resolve_language("java", tmp_path) == "java"  # an explicit setting wins over detection
    assert resolve_language("JS", tmp_path) == "javascript"
    with pytest.raises(ValueError, match="PROJECT_LANGUAGE must be auto, python, javascript, java"):
        resolve_language("rust", tmp_path)


@pytest.mark.parametrize("language", ["python", "javascript", "java"])
def test_language_picks_the_test_defaults(monkeypatch, tmp_path: Path, language: str) -> None:
    for name in ("TEST_COMMAND", "TEST_COMMAND_ALLOWLIST", "SANDBOX_IMAGE", "GITHUB_ISSUES_FILE"):
        monkeypatch.delenv(name, raising=False)
    repo = setup_sample_repo(tmp_path / "repo", language=language, with_origin=False)
    monkeypatch.setenv("REPO_PATH", str(repo))
    monkeypatch.setenv("PROJECT_LANGUAGE", "auto")
    settings = Settings.from_env(tmp_path / "no.env")
    profile = PROFILES[language]
    assert settings.project_language == language
    assert (settings.test_command, settings.test_command_allowlist, settings.sandbox_image) == (profile.test_command, profile.test_command_allowlist, profile.sandbox_image)
    assert settings.github_issues_file == PROJECT_ROOT / "examples" / "issues" / profile.issues_file


def test_explicit_test_settings_still_win(monkeypatch, tmp_path: Path) -> None:
    repo = setup_sample_repo(tmp_path / "repo", language="javascript", with_origin=False)
    monkeypatch.setenv("REPO_PATH", str(repo))
    monkeypatch.setenv("TEST_COMMAND", "node --test test/sessions.test.js")
    monkeypatch.setenv("TEST_COMMAND_ALLOWLIST", "node --test")
    monkeypatch.setenv("SANDBOX_IMAGE", "my-node-image:1")
    settings = Settings.from_env(tmp_path / "no.env")
    assert settings.project_language == "javascript"
    assert (settings.test_command, settings.test_command_allowlist, settings.sandbox_image) == ("node --test test/sessions.test.js", ("node --test",), "my-node-image:1")


def test_runner_is_chosen_by_the_command() -> None:
    assert runner_for(["pytest", "-q"]) == runner_for(["python", "-m", "pytest"]) == "pytest"
    assert runner_for(["node", "--test"]) == "node"
    assert runner_for(["mvn", "-o", "test"]) == "maven"
    assert runner_for([]) == "pytest"


# --------------------------------------------------------------------------- reading test output


@pytest.mark.parametrize(
    "name, runner, kind, failing, summary",
    [
        ("node-failure.txt", "node", "test_failures", ["refresh rotates the refresh token"], "5 tests: 4 passed, 1 failed"),
        ("node-spec-failure.txt", "node", "test_failures", ["refresh rotates the refresh token"], "5 tests: 4 passed, 1 failed"),
        ("node-syntax-error.txt", "node", "collection_error", ["test/broken.test.js", "refresh rotates the refresh token"], "6 tests: 4 passed, 2 failed"),
        ("node-no-tests.txt", "node", "no_tests", [], "no tests ran"),
        ("node-pass.txt", "node", "passed", [], "5 tests: 5 passed, 0 failed"),
        ("maven-failure.txt", "maven", "test_failures", ["authkit.SessionStoreTest.refreshRotatesRefreshToken"], "Tests run: 5, Failures: 1, Errors: 0, Skipped: 0"),
        ("maven-compile-error.txt", "maven", "collection_error", [], "compilation failed"),
        ("maven-no-tests.txt", "maven", "no_tests", [], "no tests ran"),
        ("maven-pass.txt", "maven", "passed", [], "Tests run: 5, Failures: 0, Errors: 0, Skipped: 0"),
    ],
)
def test_recorded_runner_output_is_read_correctly(name: str, runner: str, kind: str, failing: list[str], summary: str) -> None:
    exit_code, output = recorded(name)
    argv = ["node", "--test"] if runner == "node" else ["mvn", "-o", "-B", "test"]
    text = format_result(ExecResult(argv, exit_code, output, "", 1.0, False), "docker", 120)
    status = text.splitlines()[0].removeprefix("status: ")
    assert status == ("PASSED" if kind == "passed" else "FAILED")  # no tests at all is not a pass, even with exit code 0
    assert f"summary: {summary}" in text
    assert analyze(status, text, exit_code, runner) == {"failure_kind": kind, "failing_tests": failing}


def test_nested_node_suites_name_the_test_by_its_path() -> None:
    tap = """TAP version 13
# Subtest: SessionStore
    # Subtest: refresh
        # Subtest: rotates the token
        not ok 1 - rotates the token
          ---
          failureType: 'testCodeFailure'
          ...
        1..1
    not ok 1 - refresh
      ---
      failureType: 'subtestsFailed'
      ...
    # Subtest: logout
    ok 2 - logout
not ok 1 - SessionStore
  ---
  failureType: 'subtestsFailed'
  ...
# Subtest: top level
not ok 2 - top level
  ---
  failureType: 'testCodeFailure'
  ...
# tests 3
# pass 1
# fail 2
"""
    parsed = parse_output(tap, "node")
    assert parsed["failing"] == ["SessionStore > refresh > rotates the token", "top level"]  # suites are not tests
    assert parsed["summary"] == "3 tests: 1 passed, 2 failed" and not parsed["collection_error"]


def test_older_surefire_output_is_read_too() -> None:
    output = "refreshRotates(authkit.SessionStoreTest)  Time elapsed: 0.01 sec  <<< FAILURE!\nTests run: 5, Failures: 1, Errors: 0, Skipped: 0\n"
    assert parse_output(output, "maven")["failing"] == ["authkit.SessionStoreTest.refreshRotates"]


def test_pytest_output_reading_is_unchanged() -> None:
    output = "FAILED tests/test_sessions.py::test_refresh_rotates_refresh_token - assert 1 == 2\n===== 1 failed, 4 passed in 0.05s =====\n"
    parsed = parse_output(output)
    assert parsed["failing"] == ["tests/test_sessions.py::test_refresh_rotates_refresh_token"]
    assert parsed["summary"] == "1 failed, 4 passed in 0.05s"
    assert analyze("FAILED", output, 5, "pytest")["failure_kind"] == "no_tests"  # pytest's own exit code for it


# --------------------------------------------------------------------------- samples


@pytest.mark.parametrize("language, files", [("javascript", ["package.json", "src/sessions.js", "test/sessions.test.js"]), ("java", ["pom.xml", "src/main/java/authkit/SessionStore.java", "src/test/java/authkit/SessionStoreTest.java"])])
def test_samples_are_set_up_as_git_repositories(tmp_path: Path, language: str, files: list[str]) -> None:
    repo = setup_sample_repo(tmp_path / "repo", language=language)
    assert all((repo / f).is_file() for f in files)
    assert detect_language(repo) == language
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True)
    assert status.stdout == ""
    with pytest.raises(ValueError, match="no sample repository"):
        setup_sample_repo(tmp_path / "other", language="rust")


@pytest.mark.parametrize("language", ["javascript", "java"])
def test_sample_issue_42_points_at_files_that_exist(language: str) -> None:
    profile = PROFILES[language]
    issue = json.loads((PROJECT_ROOT / "examples" / "issues" / profile.issues_file).read_text())["issues"][0]
    sample = PROJECT_ROOT / "examples" / profile.sample
    mentioned = [p for p in ("src/sessions.js", "test/sessions.test.js", "src/main/java/authkit/SessionStore.java", "src/test/java/authkit/SessionStoreTest.java") if p in issue["body"] + issue["comments"][0]["body"]]
    assert len(mentioned) == 2 and all((sample / p).is_file() for p in mentioned)


def test_java_image_prefetches_the_versions_the_sample_uses() -> None:
    import xml.etree.ElementTree as ET

    ns = {"m": "http://maven.apache.org/POM/4.0.0"}

    def versions(pom: Path) -> dict[str, str]:
        root = ET.parse(pom).getroot()
        found = {p.findtext("m:artifactId", namespaces=ns): p.findtext("m:version", namespaces=ns) for p in root.iterfind("m:build/m:plugins/m:plugin", ns)}
        found["junit"] = root.findtext("m:properties/m:junit.version", namespaces=ns)
        found["release"] = root.findtext("m:properties/m:maven.compiler.release", namespaces=ns)
        return found

    sample = versions(PROJECT_ROOT / "examples" / "sample-repo-java" / "pom.xml")
    assert sample == versions(PROJECT_ROOT / "sandbox" / "java" / "prefetch" / "pom.xml")  # otherwise offline Maven can't find them
    assert {"maven-resources-plugin", "maven-compiler-plugin", "maven-surefire-plugin"} <= set(sample)


# --------------------------------------------------------------------------- a JavaScript run end to end

JS_PLAN = {
    "summary": "SessionStore.refresh does not rotate refresh tokens.",
    "steps": ["Read refresh in src/sessions.js", "Register the new refresh token and revoke the used one", "Add a regression test"],
    "search_queries": ["refresh(", "_byRefresh"],
    "acceptance_criteria": ["A used refresh token is rejected", "The new refresh token can be used"],
    "files_to_change": ["src/sessions.js", "test/sessions.test.js"],
}

JS_REGRESSION_TEST = """
test('refreshing twice in a row works', () => {
  const store = new SessionStore();
  const first = store.refresh(store.login('alice').refreshToken);
  const second = store.refresh(first.refreshToken);
  assert.equal(store.authenticate(second.accessToken), 'alice');
});
"""


def unified_diff(path: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}"))


@pytest.fixture
def js_settings(tmp_path: Path) -> Settings:
    repo = setup_sample_repo(tmp_path / "sample-repo-js", language="javascript")
    return make_settings(
        repo,
        tmp_path / "data",
        writable_paths=("src", "test"),
        github_repository="demo/sample-repo-js",
        github_issues_file=PROJECT_ROOT / "examples" / "issues" / "sample-repo-js.json",
        sandbox_image="mcp-agent-sandbox-js:latest",
        test_command="node --test --test-reporter=tap",
        test_command_allowlist=("node --test",),
        project_language="javascript",
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to stand in for the JavaScript sandbox image")
async def test_javascript_issue_from_failing_tests_to_publication(js_settings: Settings) -> None:
    repo = js_settings.repo_path
    source, tests = (repo / "src/sessions.js").read_text(), (repo / "test/sessions.test.js").read_text()
    fixed = source.replace(
        "    this._byAccess.delete(session.accessToken);\n",
        "    this._byRefresh.delete(refreshToken);\n    this._byAccess.delete(session.accessToken);\n",
    ).replace(
        "    this._byAccess.set(newSession.accessToken, newSession);\n    return newSession;",
        "    this._byAccess.set(newSession.accessToken, newSession);\n    this._byRefresh.set(newSession.refreshToken, newSession);\n    return newSession;",
    )
    provider = ScriptedProvider(
        plan=JS_PLAN,
        turns={
            "investigate": [[("read_repository_file", {"path": "src/sessions.js"})], "Root cause: refresh never stores the new refresh token or revokes the used one."],
            "propose": [
                # first attempt: only the regression test, so the tests fail
                [("propose_patch", {"path": "test/sessions.test.js", "patch": unified_diff("test/sessions.test.js", tests, tests + JS_REGRESSION_TEST)})],
                "Add a regression test for refreshing twice.",
                # second attempt, after the failing run: the fix
                [("propose_patch", {"path": "src/sessions.js", "patch": unified_diff("src/sessions.js", source, fixed)})],
                "Store the new refresh token and revoke the used one.",
            ],
        },
    )
    sandbox = LocalPytestRunner()
    runner, _ = make_runner(js_settings, provider, sandbox)
    view = await runner.start(42)
    assert "```js" in view["state"]["issue"]  # the JavaScript version of the issue
    view = await runner.resume(view["run"]["id"], approved=True)
    first = view["state"]["test_runs"][0]
    assert (first["status"], first["failure_kind"], first["summary"]) == ("FAILED", "test_failures", "6 tests: 4 passed, 2 failed")
    assert first["failing_tests"] == ["refresh rotates the refresh token", "refreshing twice in a row works"]
    assert view["pending_approval"]["kind"] == "patch_approval"
    view = await runner.resume(view["run"]["id"], approved=True)
    last = view["state"]["test_runs"][-1]
    assert (last["status"], last["summary"]) == ("PASSED", "6 tests: 6 passed, 0 failed")
    assert view["pending_approval"]["kind"] == "publish_approval"
    assert sandbox.commands == [["node", "--test", "--test-reporter=tap"]] * 2


# --------------------------------------------------------------------------- real Docker


def _image_ready(image: str) -> bool:
    if not shutil.which("docker"):
        return False
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
        found = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and found.returncode == 0


@pytest.mark.docker
@pytest.mark.skipif(not _image_ready("mcp-agent-sandbox-js:latest"), reason="needs Docker and mcp-agent-sandbox-js:latest (docker build -t mcp-agent-sandbox-js:latest sandbox/javascript/)")
def test_real_docker_run_javascript(tmp_path: Path) -> None:
    repo = setup_sample_repo(tmp_path / "repo", language="javascript", with_origin=False)
    runner = DockerRunner("mcp-agent-sandbox-js:latest", timeout_seconds=120)
    result = runner.run(repo, ["node", "--test", "--test-reporter=tap"])
    text = format_result(result, runner.name, 120)
    assert "status: FAILED" in text and "summary: 5 tests: 4 passed, 1 failed" in text and not result.stderr.strip()
    assert analyze("FAILED", text, result.exit_code, "node")["failing_tests"] == ["refresh rotates the refresh token"]
    connect = "require('net').connect(53, '1.1.1.1').on('connect', () => process.exit(0)).on('error', () => process.exit(3))"
    assert runner.run(repo, ["node", "-e", connect]).exit_code == 3  # no network


@pytest.mark.docker
@pytest.mark.skipif(not _image_ready("mcp-agent-sandbox-java:latest"), reason="needs Docker and mcp-agent-sandbox-java:latest (docker build -t mcp-agent-sandbox-java:latest sandbox/java/)")
def test_real_docker_run_java(tmp_path: Path) -> None:
    repo = setup_sample_repo(tmp_path / "repo", language="java", with_origin=False)
    runner = DockerRunner("mcp-agent-sandbox-java:latest", timeout_seconds=120)
    result = runner.run(repo, ["mvn", "-o", "-B", "test"])  # offline: everything comes from the image
    text = format_result(result, runner.name, 120)
    assert "status: FAILED" in text and "summary: Tests run: 5, Failures: 1, Errors: 0, Skipped: 0" in text
    assert not result.stderr.strip()
    assert analyze("FAILED", text, result.exit_code, "maven")["failing_tests"] == ["authkit.SessionStoreTest.refreshRotatesRefreshToken"]
