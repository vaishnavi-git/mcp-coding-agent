# Setup

Two ways to run the agent:

- **Local install (recommended).** The API and UI run on your machine; only the tests run in
  Docker containers.
- **Docker Compose.** The API and UI run in containers too. This needs the Docker socket inside
  the API container, which is equivalent to root on the host.

Either way you need Docker for the tests (the agent never runs tests outside Docker) and an
Anthropic API key.

## Local install

Requirements: Python 3.11+, git, Docker (Desktop or Engine), an Anthropic API key.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[ui,dev]"
cp .env.example .env          # set ANTHROPIC_API_KEY
python scripts/setup_sample_repo.py --force
docker build -t mcp-agent-sandbox:latest sandbox/
```

`setup_sample_repo.py` copies `examples/sample-repo` to `workspaces/sample-repo`, runs `git
init`, and creates a local bare repository as its `origin`. In the default `GITHUB_MODE=fixture`,
issues come from [examples/issues/sample-repo.json](../examples/issues/sample-repo.json), pushes
go to that local remote, and pull requests are written to `data/pull_requests/`, so the whole
flow runs without GitHub.

Start the API and the UI in two terminals:

```bash
uvicorn app.api.main:app --port 8000
```

```bash
streamlit run app/ui/streamlit_app.py
```

Open http://127.0.0.1:8501 and start a run for issue **#42**. The interactive API docs are at
http://127.0.0.1:8000/docs. Both servers listen on 127.0.0.1 and have no authentication: do not
expose them on a network.

To start over, recreate the sample repository (`python scripts/setup_sample_repo.py --force`).
Runs change its working tree, and a new run expects a clean one.

### JavaScript and Java

The same sample exists in JavaScript (Node's built-in test runner) and Java (Maven and JUnit 5).
Each language has its own test image:

```bash
python scripts/setup_sample_repo.py --language javascript --force   # workspaces/sample-repo-js
docker build -t mcp-agent-sandbox-js:latest sandbox/javascript/

python scripts/setup_sample_repo.py --language java --force         # workspaces/sample-repo-java
docker build -t mcp-agent-sandbox-java:latest sandbox/java/
```

Then point `REPO_PATH` at the new directory and set `WRITABLE_PATHS` to `src,test` (JavaScript) or
`src` (Java). `PROJECT_LANGUAGE` is detected from the repository (`package.json`, `pom.xml`), and it
picks the test command, the allowlist, the sandbox image and the fixture issues. If your `.env` was
copied from an older `.env.example`, remove the `TEST_COMMAND`, `TEST_COMMAND_ALLOWLIST`,
`SANDBOX_IMAGE` and `GITHUB_ISSUES_FILE` lines, which would otherwise keep the Python values.

The test sandbox has no network, so dependencies have to be in the image. The Java image downloads
JUnit and the Maven plugins the sample uses while it's built, and Maven runs offline (`-o`). For
your own Java project, add what it needs to `sandbox/java/prefetch/pom.xml`; for a JavaScript
project with dependencies, see the comment in `sandbox/javascript/Dockerfile`.

## Docker Compose

```bash
cp .env.example .env                 # set ANTHROPIC_API_KEY; set UID/GID (id -u, id -g) and DOCKER_GID
mkdir -p /tmp/mcp-agent-sandbox      # SANDBOX_WORK_DIR, see below
docker compose build                 # app image and the sandbox test image
docker compose run --rm api python scripts/setup_sample_repo.py --force
docker compose up
```

Then open http://127.0.0.1:8501. What the compose file does:

- **`api`**
  - runs the FastAPI server and the four MCP servers;
  - mounts `./workspaces`, `./data`, the Docker socket and `SANDBOX_WORK_DIR`;
  - publishes port 8000 on 127.0.0.1 only.
- **`ui`** runs Streamlit against `http://api:8000`, on 127.0.0.1:8501.
- **`sandbox`** builds `mcp-agent-sandbox:latest` and exits. The API starts a fresh container
  from it for every test run, with no network, a read-only root, no capabilities and resource
  limits.

Things to know:

- **The Docker socket.** The API needs the socket to start test containers. Anything that can
  use the socket controls the host. Prefer the local install where that matters.
- **`SANDBOX_WORK_DIR`.** The API copies the repository into this directory, and the Docker
  daemon then bind-mounts the copy into the test container *by host path*. That only works if
  the directory has the same path on the host and in the API container, which is how the compose
  file mounts it. Create it before the first `up`: if Docker creates it, root owns it.
- **User IDs.** Build with your own user and group IDs (`UID`, `GID` in `.env`, or
  `UID=$(id -u) GID=$(id -g) docker compose build`), so the container can run git on the mounted
  repository without "dubious ownership" errors. On macOS your group is usually 20, which the
  image already has, and that's handled.
- **`DOCKER_GID`.** The group that owns the socket on the host. On Linux:
  `stat -c %g /var/run/docker.sock`. With Docker Desktop the default (0) works.
- **Run the setup script inside the container.** The sample repository's `origin` is an
  absolute path, so it must be created where the API will use it. From a script or CI, add `-T`
  (`docker compose run --rm -T api ...`): without a terminal, `run` otherwise waits forever.
- **Cloud-synced folders.** If the project lives in a folder that iCloud Drive (or a similar
  service) syncs, such as a synced Desktop, the service creates conflict copies like `refs 2`
  inside the sample repositories' `.git` folders and can corrupt them. Set `WORKSPACES_DIR` to a
  folder outside it, for example `WORKSPACES_DIR=$HOME/mcp-agent-workspaces docker compose up`.
  Better still, keep the whole project outside synced folders.

Checked on macOS with Docker Desktop (engine 29.9.3, Compose 5.7): the build, the sample setup in
the container, `up`, the API's health check, all four MCP servers starting in the API container,
a test run in the sandbox started from the API container through the socket, git on the mounted
repository, and the UI showing recorded runs. Not checked: Linux hosts, and a full run with a
model.

## Using a real GitHub repository

1. Clone the repository somewhere and set `REPO_PATH` to the clone.
2. Set `GITHUB_MODE=api` and `GITHUB_REPOSITORY=owner/name`.
3. Create a **fine-grained** personal access token for that one repository and set
   `GITHUB_TOKEN`:

   | Permission | Level | Used by |
   |---|---|---|
   | Metadata | Read | required for every token |
   | Issues | Read | `get_github_issue` |
   | Contents | Read and write | `push_branch` |
   | Pull requests | Read and write | `create_pull_request` |

   If you never publish, `Issues: read` is enough (public repositories need no token for that).
4. Set `WRITABLE_PATHS` to the directories the agent may change. Extend `sandbox/Dockerfile`
   with the repository's test dependencies, and set `TEST_COMMAND` if it differs.
5. Recommended on GitHub: protect the default branch, and add a ruleset that limits the token's
   pushes to `agent/*` branches.

The agent never force-pushes and never pushes the default branch. Pull requests are drafts
unless `GITHUB_PR_DRAFT=false`.

## Settings

All settings are environment variables, read from `.env` if present (see
[.env.example](../.env.example)). Paths are relative to the project directory.

### Model

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key. Never logged; no MCP server receives it. |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` (in `.env.example`; nothing in code) | Model for every call. Sonnet 5.5 is the suggested default since the [2026-11-17 benchmark](EVALUATION.md#benchmark-of-2026-11-17-sonnet-5-and-sonnet-55): as accurate as Sonnet 5 there, a third cheaper and twice as fast. |
| `ANTHROPIC_MAX_TOKENS` | `16000` | Maximum output tokens per call. |
| `ANTHROPIC_EFFORT` | (unset) | `low`, `medium`, `high`, `xhigh` or `max`. |
| `ANTHROPIC_THINKING` | `adaptive` | `adaptive` or `none`. |
| `ANTHROPIC_TIMEOUT_SECONDS` | `120` | Per-request timeout. |
| `ANTHROPIC_MAX_RETRIES` | `3` | Retries of 429, 5xx, 529 and timeouts, with backoff (the SDK's own retries are off). |
| `ANTHROPIC_MODEL_PLAN`, `ANTHROPIC_MODEL_INVESTIGATE`, `ANTHROPIC_MODEL_PROPOSE`, `ANTHROPIC_MODEL_PUBLISH`, `ANTHROPIC_MODEL_SUMMARY` | (unset: `ANTHROPIC_MODEL`) | A different model for one step. `PLAN` covers choosing the searches and writing the plan; `PUBLISH` covers drafting and checking the pull request text. Each call is priced at the model it used, so with a cost limit every model needs a price. |
| `ANTHROPIC_EFFORT_PLAN`, `ANTHROPIC_EFFORT_INVESTIGATE`, `ANTHROPIC_EFFORT_PROPOSE`, `ANTHROPIC_EFFORT_PUBLISH`, `ANTHROPIC_EFFORT_SUMMARY` | (unset: `ANTHROPIC_EFFORT`) | A different effort level for one step. |

### Repository and GitHub

| Variable | Default | Meaning |
|---|---|---|
| `REPO_PATH` | `workspaces/sample-repo` | The only repository the agent can read or change. |
| `PROJECT_LANGUAGE` | `auto` | `python`, `javascript` or `java`; `auto` looks for `pyproject.toml`, `setup.py`, `setup.cfg` or `requirements.txt`, then `package.json`, then `pom.xml` when the agent starts. Picks the defaults marked "by language". |
| `WRITABLE_PATHS` | (whole repository) | Comma-separated directories the agent may change. |
| `ALLOW_NEW_FILES` | `true` | Whether patches may create files (inside writable paths). |
| `GITHUB_MODE` | `fixture` | `fixture` (issues from a JSON file, local remote) or `api` (GitHub). |
| `GITHUB_REPOSITORY` | `demo/sample-repo` | `owner/name`. |
| `GITHUB_TOKEN` | (unset) | Fine-grained token; only the GitHub MCP server receives it. |
| `GITHUB_ISSUES_FILE` | by language: `examples/issues/sample-repo.json`, `sample-repo-js.json`, `sample-repo-java.json` | Issues for fixture mode. |
| `GITHUB_API_URL` | `https://api.github.com` | For GitHub Enterprise Server. |
| `GITHUB_WEB_URL` | `https://github.com` | Push URL base for api mode. |
| `GITHUB_DEFAULT_BRANCH` | `main` | Never committed to or pushed directly; pull requests target it. |
| `GITHUB_BRANCH_PREFIX` | `agent/` | Required prefix of new branches (empty disables the rule). |
| `GITHUB_PR_DRAFT` | `true` | Open pull requests as drafts. |
| `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` | `MCP Coding Agent`, a noreply address | Commit identity. |

### Test sandbox

| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_IMAGE` | by language: `mcp-agent-sandbox:latest`, `mcp-agent-sandbox-js:latest`, `mcp-agent-sandbox-java:latest` | Test image, built from `sandbox/`, `sandbox/javascript/` or `sandbox/java/`. |
| `SANDBOX_TIMEOUT_SECONDS` | `120` | A test run is killed after this. |
| `SANDBOX_MEMORY_MB`, `SANDBOX_CPUS`, `SANDBOX_PIDS` | `1024`, `1.0`, `256` | Container limits. |
| `SANDBOX_WORK_DIR` | system temp directory | Where repository copies for test runs go (see Docker Compose). |
| `TEST_COMMAND` | by language: `pytest -q -p no:cacheprovider`, `node --test --test-reporter=tap`, `mvn -o -B test` | The command the workflow runs. Checked against the allowlist like any other. |
| `TEST_COMMAND_ALLOWLIST` | by language: `pytest,python -m pytest`, `node --test`, `mvn` | Allowed command prefixes. Each runner also has its own rules for arguments: see [SECURITY.md](SECURITY.md#controls). |

### Documentation reader

| Variable | Default | Meaning |
|---|---|---|
| `DOCS_ENABLED` | `true` | Start the docs server and offer its tools to the model. |
| `DOCS_SOURCES_FILE` | `config/docs_sources.json` | The only sites it can read. Review changes like code. |
| `DOCS_TIMEOUT_SECONDS` | `15` | Per request. |
| `DOCS_MAX_PAGE_BYTES` | `2000000` | Larger pages are cut off. |
| `DOCS_MAX_FETCHES` | `40` | Network requests per workflow step. |
| `DOCS_RESPECT_ROBOTS` | `true` | Honor robots.txt. |
| `DOCS_CACHE_TTL_SECONDS` | `86400` | Search-index cache lifetime. |

### Limits and reliability

| Variable | Default | Meaning |
|---|---|---|
| `MAX_TOOL_CALLS_PER_STEP` | `25` | Tool calls the model may request in one step, refused ones included. |
| `MAX_FIX_ATTEMPTS` | `2` | Fix attempts after failing tests before the recovery checkpoint. |
| `MAX_REVISIONS` | `2` | Revisions after a rejection with feedback. |
| `MAX_TASK_COST_USD` | `5.00` | Cost limit per run (0 disables). Needs a price for the model. |
| `MAX_LLM_CALLS_PER_RUN` | `100` | Model-call limit per run (0 disables). |
| `MAX_TOOL_CALLS_PER_RUN` | `150` | Tool-call limit per run (0 disables). |
| `MODEL_PRICES_FILE` | `config/model_prices.json` | USD per million tokens, per model. |
| `LLM_PRICE_INPUT_PER_MTOK`, `LLM_PRICE_OUTPUT_PER_MTOK`, `LLM_PRICE_CACHE_READ_PER_MTOK`, `LLM_PRICE_CACHE_WRITE_PER_MTOK` | (unset) | Override the price file (input and output both needed). |
| `TOOL_TIMEOUT_SECONDS` | `60` | Per-call deadline for the GitHub and filesystem servers. |
| `TOOL_MAX_RETRIES` | `2` | Retries of temporary errors, read-only and idempotent tools only. |
| `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS` | `1.0`, `30` | Exponential backoff bounds. |
| `MAX_RUN_RETRIES` | `3` | Retries of a failed or interrupted run. |
| `REQUIRE_PLAN_APPROVAL` | `false` | Add a checkpoint to approve the plan before investigation. |
| `CHECK_PR_SUMMARY` | `true` | Fact-check the drafted pull request text against the issue, the diff and the test runs, and correct it (one extra model call per draft). |

### Storage, UI and Docker Compose

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `data` | Audit database, checkpoints, docs cache, fixture pull requests. |
| `AGENT_API_URL` | `http://127.0.0.1:8000` | Where the UI finds the API. |
| `UID`, `GID` | `1000` | Compose only: user the app image runs as. |
| `DOCKER_GID` | `0` | Compose only: group that owns the Docker socket. |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Docker is not installed on this host" although Docker Desktop is running | Docker Desktop may put the `docker` command in `~/.docker/bin` without adding it to your shell's path. Add `export PATH="$HOME/.docker/bin:$PATH"` to `~/.zshrc`, or let Docker Desktop install its tools system-wide (Settings, Advanced), then restart the API from a new terminal. |
| The run waits at the recovery checkpoint with "tests could not run" | Docker is not running or the sandbox image is missing. Start Docker, build the image, then choose **Retry** to run the tests again. |
| `pricing_unknown` when starting a run | `MAX_TASK_COST_USD` is set but the model has no price. Add it to `config/model_prices.json`, set `LLM_PRICE_*`, or set `MAX_TASK_COST_USD=0`. |
| `budget_exceeded` | A run limit was reached. The failure says which; raise it and retry the run. |
| "patch headers refer to …" or "does not apply" | The model's patch did not match the file. It is told why and usually corrects it; rejecting with feedback also works. |
| `apply_failed` with "nothing was written" | A file changed after the patch was proposed. Start a new run on a clean working tree. |
| "fixture mode pushes to the 'origin' remote, but the repository has none" | Recreate the sample repository with `python scripts/setup_sample_repo.py --force`. |
| GitHub "rate limit exceeded" | Set `GITHUB_TOKEN`; temporary limits are retried automatically when the wait is short. |
