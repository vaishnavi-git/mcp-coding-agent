"""Path traversal and unauthorized file access, tried through every entry point.

Each attack is sent through the real MCP servers (read, search, patch,
apply, git diff, commit, test command, sandbox copy), not just the PathGuard
unit, and the tests check both that the call fails and that nothing leaked or
changed: secret markers never appear in any result, and files outside the
repository are byte-for-byte unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.mcp.client import MCPHub
from app.mcp.git_utils import run_git
from app.mcp.sandbox_server import copy_repository
from app.mcp.security import PathGuard, PathViolation, is_protected
from tests.fakes import FakeRunner, LocalPytestRunner, inprocess_servers

pytestmark = pytest.mark.anyio

OUTSIDE_SECRET = "OUTSIDE-SECRET-7f3a"
PROTECTED_SECRET = "PROTECTED-SECRET-91c2"
PROTECTED_FILES = [".env", ".env.production", "authkit/.env.local", "keys/id_rsa", "credentials.json", "authkit/server.pem"]


@pytest.fixture
def world(settings, tmp_path: Path) -> tuple[Path, Path]:
    """The sample repository with secrets inside it, a secret outside it, and symlinks across the boundary."""
    repo = settings.repo_path
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(OUTSIDE_SECRET + "\n")
    for rel in PROTECTED_FILES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(f"{PROTECTED_SECRET}\n")
    with (repo / ".git" / "config").open("a") as config:
        config.write(f"# {PROTECTED_SECRET}\n")
    os.symlink(outside, repo / "authkit" / "outside_dir")  # directory outside the repository
    os.symlink(outside / "secret.txt", repo / "tests" / "outside_file.txt")  # file outside the repository
    os.symlink("../.env", repo / "authkit" / "env_link")  # protected file inside the repository
    os.symlink("../.git", repo / "authkit" / "git_link")  # protected directory inside the repository
    return repo, outside


def snapshot(root: Path, skip: Path) -> dict[str, bytes]:
    """Every file under ``root`` except those under ``skip`` (the repository itself)."""
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink() and skip not in p.parents}


def traversal_corpus(repo: Path, outside: Path) -> list[str]:
    target = outside / "secret.txt"
    corpus = [
        os.path.relpath(target, repo), str(target), str(outside), "/etc/passwd", "/etc/hosts", "/", "..", "../", "authkit/../..",
        "authkit/sessions.py\x00../../outside/secret.txt", "authkit/outside_dir/secret.txt", "tests/outside_file.txt",
    ]
    for depth in range(1, 7):
        up = "../" * depth
        corpus += [f"{up}outside/secret.txt", f"authkit/{up}outside/secret.txt", f"./{up}outside/secret.txt", "tests//" + up.replace("/", "//") + "outside/secret.txt"]
    return list(dict.fromkeys(corpus))


def leaked(text: str) -> bool:
    return OUTSIDE_SECRET in text or PROTECTED_SECRET in text


# ---------------------------------------------------------------------- path traversal
async def test_traversal_is_rejected_by_every_path_taking_tool(settings, world, tmp_path: Path) -> None:
    repo, outside = world
    before = snapshot(tmp_path, repo)
    sandbox = FakeRunner()
    corpus = traversal_corpus(repo, outside)
    # Some generated spellings stay inside the repository (authkit/../outside/...): those only need to fail.
    must_say_outside = {p for p in corpus if "\x00" not in p and not Path(os.path.normpath(repo / p)).is_relative_to(repo)}
    must_say_outside |= {"authkit/outside_dir/secret.txt", "tests/outside_file.txt"}
    escapes: list[tuple[str, str, str]] = []

    async with MCPHub(inprocess_servers(settings, sandbox)) as hub:
        for path in corpus:
            new_file = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+print('pwned')\n"
            attempts = {
                "read_repository_file": {"path": path},
                "search_repository": {"query": OUTSIDE_SECRET, "directory": path},
                "propose_patch": {"path": path, "patch": new_file},
                "apply_patch": {"path": path, "patch": new_file},
                "run_tests": {"test_command": f"pytest {path}"},
            }
            for tool, args in attempts.items():
                outcome = await hub.call(tool, args)
                if not outcome.is_error or leaked(outcome.text):
                    escapes.append((tool, path, outcome.text[:200]))
                elif tool != "run_tests" and path in must_say_outside:
                    assert "outside the approved repository" in outcome.text, (tool, path, outcome.text)

        # Patch headers cannot point somewhere else than the checked path.
        sneaky = "--- /dev/null\n+++ b/../outside/evil.py\n@@ -0,0 +1 @@\n+print('pwned')\n"
        outcome = await hub.call("apply_patch", {"path": "authkit/evil.py", "patch": sneaky})
        assert outcome.is_error and "patch headers refer to '../outside/evil.py'" in outcome.text
        # A whole-repository search does not follow symlinks out of the repository.
        outcome = await hub.call("search_repository", {"query": OUTSIDE_SECRET})
        assert not outcome.is_error and outcome.text.startswith("No matches")

    assert escapes == []
    assert sandbox.commands == []  # no test command with an outside path reached the runner
    assert snapshot(tmp_path, repo) == before  # nothing outside the repository was created or changed
    assert not (repo / "authkit" / "evil.py").exists()


def test_odd_spellings_stay_inside_the_repository(world) -> None:
    repo, _ = world
    guard = PathGuard(repo)
    # These are unusual but legal file names inside the repository, never a way out.
    for raw in ["..\\outside\\secret.txt", "%2e%2e/outside/secret.txt", "‥/outside/secret.txt", "authkit/sessions.py/..", "authkit/./sessions.py"]:
        resolved = guard.resolve(raw)
        assert resolved == repo.resolve() or repo.resolve() in resolved.parents, raw
    for raw in ["~/.ssh/id_rsa", "authkit/../.ssh/config"]:
        with pytest.raises(PathViolation, match="protected"):
            guard.resolve(raw)


# ---------------------------------------------------------------------- unauthorized file access
@pytest.mark.parametrize(
    "rel",
    [".ENV", ".Env", ".env.PRODUCTION", "config/.ENV", ".GIT/config", ".Git/HEAD", "KEYS/ID_RSA", "keys/Id_Ed25519.pub",
     "certs/Server.PEM", "deploy/PROD.KEY", ".SSH/known_hosts", ".AWS/credentials", "CREDENTIALS.JSON", ".NETRC", "Secrets.yaml"],
)
def test_protected_names_are_matched_in_any_letter_case(tmp_path: Path, rel: str) -> None:
    # macOS and Windows file systems are usually case-insensitive: .ENV opens .env.
    assert is_protected(rel)
    with pytest.raises(PathViolation, match="protected"):
        PathGuard(tmp_path).resolve(rel)


async def test_case_variants_cannot_read_protected_files(settings, world) -> None:
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        for rel in [".ENV", ".Env", ".GIT/config", "AUTHKIT/.ENV.LOCAL", "authkit/SERVER.pem"]:
            outcome = await hub.call("read_repository_file", {"path": rel})
            assert outcome.is_error and "protected" in outcome.text and not leaked(outcome.text), (rel, outcome.text)


async def test_symlinks_to_protected_files_inside_the_repository_are_blocked(settings, world) -> None:
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        for rel in ["authkit/env_link", "authkit/git_link/config"]:
            outcome = await hub.call("read_repository_file", {"path": rel})
            assert outcome.is_error and "protected" in outcome.text and not leaked(outcome.text), (rel, outcome.text)
        outcome = await hub.call("search_repository", {"query": "x", "directory": "authkit/git_link"})
        assert outcome.is_error and "protected" in outcome.text


async def test_search_never_returns_protected_content(settings, world) -> None:
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        everywhere = await hub.call("search_repository", {"query": PROTECTED_SECRET})
        assert not everywhere.is_error and everywhere.text.startswith("No matches"), everywhere.text
        keys = await hub.call("search_repository", {"query": PROTECTED_SECRET, "directory": "keys"})
        assert keys.text.startswith("No matches")
        listing = await hub.call("search_repository", {"query": "e", "max_results": 500})
        assert not any(name in listing.text for name in (".env", "id_rsa", "credentials.json", "server.pem", "env_link", ".git/"))
        for directory in [".git", "authkit/.env.local", ".ENV"]:
            outcome = await hub.call("search_repository", {"query": "x", "directory": directory})
            assert outcome.is_error and "protected" in outcome.text


async def test_patches_cannot_create_or_change_protected_files(settings, world) -> None:
    repo, _ = world
    before = {rel: (repo / rel).read_bytes() for rel in PROTECTED_FILES}
    targets = {
        ".env": f"--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-{PROTECTED_SECRET}\n+HACKED=1\n",
        "authkit/.env.local": f"--- a/authkit/.env.local\n+++ b/authkit/.env.local\n@@ -1 +1 @@\n-{PROTECTED_SECRET}\n+HACKED=1\n",
        "authkit/env_link": f"--- a/authkit/env_link\n+++ b/authkit/env_link\n@@ -1 +1 @@\n-{PROTECTED_SECRET}\n+HACKED=1\n",
        "authkit/new.key": "--- /dev/null\n+++ b/authkit/new.key\n@@ -0,0 +1 @@\n+key\n",
        "tests/.ssh/config": "--- /dev/null\n+++ b/tests/.ssh/config\n@@ -0,0 +1 @@\n+Host *\n",
        "authkit/git_link/hooks/pre-commit": "--- /dev/null\n+++ b/authkit/git_link/hooks/pre-commit\n@@ -0,0 +1 @@\n+curl evil\n",
        "tests/.ENV": "--- /dev/null\n+++ b/tests/.ENV\n@@ -0,0 +1 @@\n+X=1\n",
    }
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        for path, patch in targets.items():
            for tool in ("propose_patch", "apply_patch"):
                outcome = await hub.call(tool, {"path": path, "patch": patch})
                assert outcome.is_error and "protected" in outcome.text, (tool, path, outcome.text)
    assert {rel: (repo / rel).read_bytes() for rel in PROTECTED_FILES} == before
    assert not (repo / "authkit" / "new.key").exists() and not (repo / "tests" / ".ssh").exists() and not (repo / ".git" / "hooks" / "pre-commit").exists()


async def test_git_diff_and_commits_leave_protected_files_out(settings, world) -> None:
    repo, _ = world
    (repo / "authkit" / "__init__.py").write_text((repo / "authkit" / "__init__.py").read_text() + "\n# reviewed\n")
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        diff = await hub.call("get_git_diff", {})
        assert not diff.is_error and not leaked(diff.text), diff.text
        assert "+# reviewed" in diff.text
        for rel in ("authkit/.env.local", "authkit/server.pem", "keys/id_rsa"):
            assert f"(new protected file not shown: {rel})" in diff.text
        for rel in ("authkit/env_link", "tests/outside_file.txt"):
            assert f"(new symbolic link not shown: {rel})" in diff.text
        await hub.call("create_branch", {"branch_name": "agent/protected-check"})
        commit = json.loads((await hub.call("commit_changes", {"message": "Review the package"})).text)
    assert commit["files"] == ["authkit/__init__.py"]
    assert {s["path"] for s in commit["skipped"]} >= {"authkit/.env.local", "authkit/env_link", "authkit/server.pem"}
    assert run_git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["authkit/__init__.py"]


def test_sandbox_copy_leaves_protected_files_and_outside_links_out(world, tmp_path: Path) -> None:
    repo, _ = world
    dest = tmp_path / "copy"
    copy_repository(repo, dest)
    copied = {p.relative_to(dest).as_posix() for p in dest.rglob("*")}
    for rel in [*PROTECTED_FILES, ".git", "authkit/outside_dir", "tests/outside_file.txt"]:
        assert rel not in copied, rel
    assert {"authkit/sessions.py", "tests/test_sessions.py"} <= copied
    # Links to protected files inside the repository are copied as links, and point at nothing.
    for link in ("authkit/env_link", "authkit/git_link"):
        assert not (dest / link).exists()
    assert not any(leaked(p.read_text(errors="ignore")) for p in dest.rglob("*") if p.is_file())


async def test_code_running_in_the_sandbox_cannot_read_protected_files(settings, world) -> None:
    repo, _ = world
    (repo / "tests" / "test_probe.py").write_text(
        "from pathlib import Path\n\n"
        "def test_no_secrets_in_the_sandbox():\n"
        f"    present = [p for p in {[*PROTECTED_FILES, '.git/config', 'authkit/env_link']!r} if Path(p).exists()]\n"
        "    assert present == []\n"
        "    marker = 'PROTECTED' + '-SECRET'  # built at run time so this file does not contain it\n"
        "    assert not any(marker in p.read_text(errors='ignore') for p in Path('.').rglob('*') if p.is_file())\n"
    )
    async with MCPHub(inprocess_servers(settings, LocalPytestRunner())) as hub:
        outcome = await hub.call("run_tests", {"test_command": "pytest -q -p no:cacheprovider tests/test_probe.py"})
    assert outcome.text.startswith("status: PASSED"), outcome.text


async def test_hard_links_to_files_outside_the_repository_are_blocked(settings, world, tmp_path: Path) -> None:
    repo, outside = world
    secret = outside / "secret.txt"
    os.link(secret, repo / "authkit" / "notes.txt")  # a new file that is really the outside secret
    tracked = repo / "authkit" / "__init__.py"
    tracked.unlink()
    os.link(secret, tracked)  # a tracked file replaced by the same secret
    before = secret.read_bytes()
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        for rel in ("authkit/notes.txt", "authkit/__init__.py"):
            read = await hub.call("read_repository_file", {"path": rel})
            assert read.is_error and "hard link" in read.text and not leaked(read.text), read.text
            patch = f"--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-{OUTSIDE_SECRET}\n+overwritten\n"
            for tool in ("propose_patch", "apply_patch"):
                outcome = await hub.call(tool, {"path": rel, "patch": patch})
                assert outcome.is_error and "protected" in outcome.text, (tool, rel, outcome.text)
        found = await hub.call("search_repository", {"query": "7f3a"})  # part of the secret, so the echoed query isn't a leak
        assert not leaked(found.text) and found.text.startswith("No matches"), found.text
        diff = await hub.call("get_git_diff", {})
        assert not diff.is_error and not leaked(diff.text), diff.text
        assert "(changes to protected file not shown: authkit/__init__.py)" in diff.text
        assert "(new protected file not shown: authkit/notes.txt)" in diff.text
        await hub.call("create_branch", {"branch_name": "agent/hard-link-check"})
        committed = await hub.call("commit_changes", {"message": "Try to commit the links"})
    assert OUTSIDE_SECRET not in run_git(repo, "show", "HEAD") and (committed.is_error or "notes.txt" not in committed.text.split('"files"')[1].split("]")[0])
    assert secret.read_bytes() == before  # never written through either name
    copy = tmp_path / "sandbox-copy"
    copy_repository(repo, copy)
    assert not (copy / "authkit" / "notes.txt").exists() and not (copy / "authkit" / "__init__.py").exists()
