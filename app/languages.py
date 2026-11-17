"""What differs between Python, JavaScript and Java projects.

A profile says how to recognise a project, which sandbox image runs its tests, the default test
command and allowlist, and where the bundled sample and its issues live. Checking a test
command's arguments and reading the test output depend on the test runner rather than the
language, so they are keyed by ``runner_for()``: see ``app/mcp/security.py`` and
``app/reliability/test_results.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LanguageProfile:
    name: str
    markers: tuple[str, ...]  # files at the repository root that identify the language
    sandbox_image: str
    sandbox_context: str  # docker build context for the image, relative to the project root
    test_command: str
    test_command_allowlist: tuple[str, ...]
    sample: str  # bundled sample repository, under examples/
    issues_file: str  # its issues for fixture mode, under examples/issues/


PROFILES: dict[str, LanguageProfile] = {
    "python": LanguageProfile(
        name="python",
        markers=("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
        sandbox_image="mcp-agent-sandbox:latest",
        sandbox_context="sandbox",
        test_command="pytest -q -p no:cacheprovider",
        test_command_allowlist=("pytest", "python -m pytest"),
        sample="sample-repo",
        issues_file="sample-repo.json",
    ),
    "javascript": LanguageProfile(
        name="javascript",
        markers=("package.json",),
        sandbox_image="mcp-agent-sandbox-js:latest",
        sandbox_context="sandbox/javascript",
        test_command="node --test --test-reporter=tap",
        test_command_allowlist=("node --test",),
        sample="sample-repo-js",
        issues_file="sample-repo-js.json",
    ),
    "java": LanguageProfile(
        name="java",
        markers=("pom.xml",),
        sandbox_image="mcp-agent-sandbox-java:latest",
        sandbox_context="sandbox/java",
        test_command="mvn -o -B test",
        test_command_allowlist=("mvn",),
        sample="sample-repo-java",
        issues_file="sample-repo-java.json",
    ),
}

ALIASES = {"py": "python", "js": "javascript", "node": "javascript"}


def detect_language(repo: Path) -> str | None:
    """The first language whose marker file is at the root of ``repo``, in PROFILES order."""
    for name, profile in PROFILES.items():
        if any((Path(repo) / marker).is_file() for marker in profile.markers):
            return name
    return None


def resolve_language(setting: str | None, repo: Path) -> str:
    """PROJECT_LANGUAGE as a profile name; ``auto`` (the default) looks at the repository."""
    value = (setting or "auto").strip().lower()
    value = ALIASES.get(value, value)
    if value == "auto":
        return detect_language(repo) or "python"
    if value not in PROFILES:
        raise ValueError(f"PROJECT_LANGUAGE must be auto, {', '.join(PROFILES)}; got {setting!r}")
    return value


def runner_for(argv: list[str] | tuple[str, ...]) -> str:
    """Which runner a test command uses: ``node``, ``maven`` or ``pytest`` (the default)."""
    program = Path(argv[0]).name if argv else ""
    if program == "node":
        return "node"
    if program == "mvn":
        return "maven"
    return "pytest"
