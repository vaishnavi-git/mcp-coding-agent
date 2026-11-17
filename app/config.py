"""Application settings, read from environment variables (and an optional .env file).

Nothing secret or model-specific is hardcoded in application logic: the model
name comes from ANTHROPIC_MODEL and the key from ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _list(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_or_none(value: str | None) -> float | None:
    return float(value) if value is not None and value.strip() != "" else None


def _path(value: str | None, default: Path) -> Path:
    raw = Path(value).expanduser() if value else default
    return raw if raw.is_absolute() else (PROJECT_ROOT / raw)


@dataclass(frozen=True)
class Settings:
    # LLM
    anthropic_api_key: str | None
    anthropic_model: str  # empty when ANTHROPIC_MODEL is not set; a run then fails with model_not_configured
    anthropic_max_tokens: int
    anthropic_effort: str | None
    anthropic_thinking: str  # "adaptive" or "none"
    anthropic_timeout_seconds: float
    anthropic_max_retries: int  # retries with backoff by app.llm.reliable (the SDK's own retries are off)
    # Target repository
    repo_path: Path
    writable_paths: tuple[str, ...]
    allow_new_files: bool
    # GitHub
    github_mode: str  # "api" or "fixture"
    github_repository: str
    github_token: str | None
    github_issues_file: Path | None
    github_api_url: str
    # GitHub publishing
    github_default_branch: str
    github_branch_prefix: str
    github_pr_draft: bool
    github_web_url: str
    git_author_name: str
    git_author_email: str
    # Sandbox
    sandbox_image: str
    sandbox_timeout_seconds: int
    sandbox_memory_mb: int
    sandbox_cpus: float
    sandbox_pids: int
    sandbox_work_dir: Path | None  # where repository copies for test runs go (None: system temp dir)
    test_command: str
    test_command_allowlist: tuple[str, ...]
    # Documentation server
    docs_enabled: bool
    docs_sources_file: Path
    docs_timeout_seconds: float
    docs_max_page_bytes: int
    docs_max_fetches: int
    docs_respect_robots: bool
    docs_cache_ttl_seconds: int
    # Workflow limits
    max_tool_calls_per_step: int
    max_fix_attempts: int
    max_revisions: int
    # Reliability
    max_tool_calls_per_run: int
    max_llm_calls_per_run: int
    max_task_cost_usd: float  # 0 disables the cost limit
    model_prices_file: Path
    llm_price_input_per_mtok: float | None
    llm_price_output_per_mtok: float | None
    llm_price_cache_read_per_mtok: float | None
    llm_price_cache_write_per_mtok: float | None
    tool_timeout_seconds: float
    tool_max_retries: int
    retry_base_delay_seconds: float
    retry_max_delay_seconds: float
    max_run_retries: int
    require_plan_approval: bool
    # Storage
    data_dir: Path

    @property
    def database_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def docs_cache_dir(self) -> Path:
        return self.data_dir / "docs_cache"

    @property
    def price_overrides(self) -> dict[str, float | None]:
        return {
            "input": self.llm_price_input_per_mtok,
            "output": self.llm_price_output_per_mtok,
            "cache_read": self.llm_price_cache_read_per_mtok,
            "cache_write": self.llm_price_cache_write_per_mtok,
        }

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> Settings:
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        e = os.environ.get
        issues_file = e("GITHUB_ISSUES_FILE")
        return cls(
            anthropic_api_key=e("ANTHROPIC_API_KEY") or None,
            anthropic_model=(e("ANTHROPIC_MODEL") or "").strip(),  # no default in code: set it in .env (see .env.example)
            anthropic_max_tokens=int(e("ANTHROPIC_MAX_TOKENS", "16000")),
            anthropic_effort=e("ANTHROPIC_EFFORT") or None,
            anthropic_thinking=(e("ANTHROPIC_THINKING") or "adaptive").lower(),
            anthropic_timeout_seconds=float(e("ANTHROPIC_TIMEOUT_SECONDS", "120")),
            anthropic_max_retries=int(e("ANTHROPIC_MAX_RETRIES", "3")),
            repo_path=_path(e("REPO_PATH"), PROJECT_ROOT / "workspaces" / "sample-repo"),
            writable_paths=_list(e("WRITABLE_PATHS")),
            allow_new_files=_bool(e("ALLOW_NEW_FILES"), True),
            github_mode=(e("GITHUB_MODE") or "fixture").lower(),
            github_repository=e("GITHUB_REPOSITORY") or "demo/sample-repo",
            github_token=e("GITHUB_TOKEN") or None,
            github_issues_file=_path(issues_file, PROJECT_ROOT) if issues_file else PROJECT_ROOT / "examples" / "issues" / "sample-repo.json",
            github_api_url=e("GITHUB_API_URL") or "https://api.github.com",
            github_default_branch=e("GITHUB_DEFAULT_BRANCH") or "main",
            github_branch_prefix=e("GITHUB_BRANCH_PREFIX", "agent/"),
            github_pr_draft=_bool(e("GITHUB_PR_DRAFT"), True),
            github_web_url=e("GITHUB_WEB_URL") or "https://github.com",
            git_author_name=e("GIT_AUTHOR_NAME") or "MCP Coding Agent",
            git_author_email=e("GIT_AUTHOR_EMAIL") or "mcp-coding-agent@users.noreply.github.com",
            sandbox_image=e("SANDBOX_IMAGE") or "mcp-agent-sandbox:latest",
            sandbox_timeout_seconds=int(e("SANDBOX_TIMEOUT_SECONDS", "120")),
            sandbox_memory_mb=int(e("SANDBOX_MEMORY_MB", "1024")),
            sandbox_cpus=float(e("SANDBOX_CPUS", "1.0")),
            sandbox_pids=int(e("SANDBOX_PIDS", "256")),
            sandbox_work_dir=_path(e("SANDBOX_WORK_DIR"), PROJECT_ROOT) if e("SANDBOX_WORK_DIR") else None,
            test_command=e("TEST_COMMAND") or "pytest -q -p no:cacheprovider",
            test_command_allowlist=_list(e("TEST_COMMAND_ALLOWLIST")) or ("pytest", "python -m pytest"),
            docs_enabled=_bool(e("DOCS_ENABLED"), True),
            docs_sources_file=_path(e("DOCS_SOURCES_FILE"), PROJECT_ROOT / "config" / "docs_sources.json"),
            docs_timeout_seconds=float(e("DOCS_TIMEOUT_SECONDS", "15")),
            docs_max_page_bytes=int(e("DOCS_MAX_PAGE_BYTES", "2000000")),
            docs_max_fetches=int(e("DOCS_MAX_FETCHES", "40")),
            docs_respect_robots=_bool(e("DOCS_RESPECT_ROBOTS"), True),
            docs_cache_ttl_seconds=int(e("DOCS_CACHE_TTL_SECONDS", "86400")),
            max_tool_calls_per_step=int(e("MAX_TOOL_CALLS_PER_STEP", "25")),
            max_fix_attempts=int(e("MAX_FIX_ATTEMPTS", "2")),
            max_revisions=int(e("MAX_REVISIONS", "2")),
            max_tool_calls_per_run=int(e("MAX_TOOL_CALLS_PER_RUN", "150")),
            max_llm_calls_per_run=int(e("MAX_LLM_CALLS_PER_RUN", "100")),
            max_task_cost_usd=float(e("MAX_TASK_COST_USD", "5.00")),
            model_prices_file=_path(e("MODEL_PRICES_FILE"), PROJECT_ROOT / "config" / "model_prices.json"),
            llm_price_input_per_mtok=_float_or_none(e("LLM_PRICE_INPUT_PER_MTOK")),
            llm_price_output_per_mtok=_float_or_none(e("LLM_PRICE_OUTPUT_PER_MTOK")),
            llm_price_cache_read_per_mtok=_float_or_none(e("LLM_PRICE_CACHE_READ_PER_MTOK")),
            llm_price_cache_write_per_mtok=_float_or_none(e("LLM_PRICE_CACHE_WRITE_PER_MTOK")),
            tool_timeout_seconds=float(e("TOOL_TIMEOUT_SECONDS", "60")),
            tool_max_retries=int(e("TOOL_MAX_RETRIES", "2")),
            retry_base_delay_seconds=float(e("RETRY_BASE_DELAY_SECONDS", "1.0")),
            retry_max_delay_seconds=float(e("RETRY_MAX_DELAY_SECONDS", "30")),
            max_run_retries=int(e("MAX_RUN_RETRIES", "3")),
            require_plan_approval=_bool(e("REQUIRE_PLAN_APPROVAL"), False),
            data_dir=_path(e("DATA_DIR"), PROJECT_ROOT / "data"),
        )
