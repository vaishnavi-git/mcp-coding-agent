# Security

The agent changes code and publishes it on GitHub, steered by a language model that reads
untrusted text: issue bodies, repository files, documentation pages and test output. This page
describes what it protects, how, and what it doesn't cover. Each security requirement I set for
the project is listed with the control that enforces it and the tests that check it, and a test
makes sure every test named here exists.

## What is protected, and from what

| Asset | Threat |
|---|---|
| Secrets on the host (`.env`, SSH keys, cloud credentials, `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`) | Read by a tool call, printed by a test, logged, or sent to a website |
| Files outside the approved repository | Read or written through `..`, absolute paths, symlinks or test commands |
| The approved repository | Changed without a human seeing and approving the exact change |
| The GitHub repository | Force pushes, pushes to the default branch, unreviewed branches or pull requests, a token with too much power |
| The host | Arbitrary commands, network access or unlimited resource use by tests |
| The run budget | A model that loops or a run that never ends |

Attackers considered:
- **A malicious issue author.** They try prompt injection through the issue text.
- **Malicious content in the repository or on a documentation page.** This also arrives as
  prompt injection, through files, pages or test output.
- **A model that is confused or persuaded.** It may request harmful tool calls.
- **Network failures and partial outages.**

The person running the agent and approving its changes is trusted.

## Controls

1. **The model only reads.** It is offered only tools that MCP discovery classifies as
   read-only (`_llm_tools` refuses anything else). `apply_patch` and the GitHub write tools are
   called only by workflow steps after a human approves, and only with the approved values,
   compared by SHA-256.
2. **Order and approvals in code.** LangGraph edges and guards make sure the issue is read before
   planning, the code is searched before patching, nothing is written without approval, and
   nothing is published before the tests pass and the publication is approved.
3. **Path guard** (`app/mcp/security.py`):
   - every path is resolved (symlinks included) and must stay inside `REPO_PATH`;
   - protected names (`.git`, `.env*`, keys, `.ssh`, `.aws`, credential files) are refused in any
     letter case;
   - writes are limited to `WRITABLE_PATHS`;
   - search and the diff never follow links to protected files or out of the repository;
   - no tool deletes files.
4. **Command policy.** The sandbox accepts only allowlisted test commands with in-repository paths.
   It refuses shell syntax, `rm`, `sudo`, `git push`, force flags, network tools, package installs
   and anything else with a reason. Each test runner has its own short list of allowed options:
   - pytest: output and selection flags, `-k` with a simple expression; no `@argument-files`;
   - `node --test`: `--test` first, a reporter, a simple `--test-name-pattern`; nothing that loads
     or evaluates other code (`--require`, `--import`, `--eval`, loaders, inspectors, `--env-file`);
   - Maven: offline (`-o`) is required, `test` is the only goal, and `-Dtest=Class#method` is the
     only property besides two fixed surefire switches.

   The model never chooses the command.
5. **Docker sandbox.** Every test run gets a disposable copy of the repository without protected
   files. The container has `--network none`, a read-only root filesystem, `--cap-drop ALL` and
   `no-new-privileges`, runs as a non-root user, has memory/CPU/PID limits, and is killed at the
   timeout.
6. **GitHub least privilege.**
   - Use a fine-grained token for one repository.
   - Only the GitHub server process receives the token, and it travels in git's environment
     config, never in argv or `.git/config`.
   - Branches must carry the `agent/` prefix. The default branch is never committed to or pushed.
   - Pushes are never forced.
   - Write tools are never retried automatically.
7. **Documentation reader, not a browser.**
   - GET only, https only, under allowlisted prefixes.
   - No cookies, credentials, JavaScript or forms.
   - Login and account paths, query strings, IP hosts, private addresses and URLs with secrets
     are refused.
   - Redirects are re-checked, and the server honors robots.txt and size and request limits.
8. **Process isolation.** Each MCP server runs as its own process with a minimal environment. No
   server receives `ANTHROPIC_API_KEY`.
9. **Redaction and audit.**
   - Every tool call, retry and refused call is logged with redacted arguments and result
     metadata, but not file contents.
   - Every model call is logged with metadata only: model, tokens, latency, cost, request id.
     Prompts and responses are not logged.
   - Every human decision is logged.
10. **Budgets and timeouts.**
    - Per-call tool deadlines.
    - Per-step and per-run tool-call limits.
    - A per-run model-call limit and cost limit.
    - Bounded retries with backoff.
    - Bounded fix attempts and revisions.
11. **The benchmark's automatic reviewer stays in the lab.** `scripts/run_benchmark.py` answers
    checkpoints itself, so it refuses to run unless `GITHUB_MODE=fixture` and the repository is
    one of the bundled samples under `workspaces/`. Its decisions are recorded as
    `decided_by: evaluator`; the API has no way to record that, and the product never uses it.
12. **Prompt-injection posture.** Issue text, files, pages and test output are framed as data in
    the prompts. Even a fully persuaded model has no tool that writes, runs commands or publishes.

## Requirements, controls and tests

### Repository workflow

| Requirement | Control | Tests |
|---|---|---|
| The agent must read an issue before creating a plan | `fetch_issue` precedes `plan`; `plan` refuses to run without the issue | `tests/test_agent_workflow.py::test_unknown_issue_fails_before_planning`, `tests/test_agent_workflow.py::test_issue_to_passing_tests_with_failed_test_recovery` |
| The agent must search the repository before editing files | `plan` runs its searches before the plan is written; `investigate` and `propose` refuse to run without them | `tests/test_agent_workflow.py::test_issue_to_passing_tests_with_failed_test_recovery`, `tests/test_agent_workflow.py::test_the_plan_is_written_after_searching_the_code`, `tests/test_agent_workflow.py::test_planning_falls_back_to_the_issue_title_and_stops_if_every_search_fails` |
| The agent must show a plan before making changes | The plan is in the run state and in the patch checkpoint; optional plan checkpoint | `tests/test_reliability_workflow.py::test_plan_approval_checkpoint`, `tests/test_agent_workflow.py::test_single_approval_success` |
| File changes must require user approval | Model gets read-only tools only; `apply` requires an approval matching each patch's SHA-256 | `tests/test_agent_workflow.py::test_write_tools_are_never_offered_to_the_model`, `tests/test_agent_workflow.py::test_rejection_with_feedback_then_without`, `tests/test_tool_contracts.py::test_tool_catalog_matches_the_reviewed_contract` |
| The agent must show the diff after every change | `apply` returns each file's diff and captures `get_git_diff` | `tests/test_agent_workflow.py::test_issue_to_passing_tests_with_failed_test_recovery`, `tests/test_filesystem_server.py::test_create_new_file_and_diff_includes_it` |
| Tests must run inside Docker | The sandbox server has only the Docker runner; without Docker the run waits at the recovery checkpoint | `tests/test_sandbox_server.py::test_docker_unavailable_is_a_clear_error`, `tests/test_agent_workflow.py::test_docker_unavailable_fails_after_apply`, `tests/test_sandbox_server.py::test_real_docker_run` |
| Test commands must use an allowlist | `validate_test_command` | `tests/test_security.py::test_dangerous_commands_are_rejected`, `tests/test_command_policy.py::test_allowlist_bypass_attempts_are_rejected`, `tests/test_command_policy.py::test_a_dangerous_configured_test_command_is_rejected_too` |
| File paths must be restricted to the approved repository | `PathGuard.resolve` in every path-taking tool | `tests/test_access_control.py::test_traversal_is_rejected_by_every_path_taking_tool`, `tests/test_security.py::test_writable_paths_are_enforced` |
| The agent must prevent path traversal | Symlinks resolved before the containment check; links out of the repository never followed | `tests/test_security.py::test_path_traversal_is_rejected`, `tests/test_security.py::test_symlink_escape_is_rejected`, `tests/test_access_control.py::test_odd_spellings_stay_inside_the_repository` |
| The agent must not read .env files, SSH keys, or files outside the repository | Protected names in any case, in reads, search, diffs, commits and the sandbox copy | `tests/test_security.py::test_forbidden_files_are_rejected`, `tests/test_access_control.py::test_protected_names_are_matched_in_any_letter_case`, `tests/test_access_control.py::test_search_never_returns_protected_content`, `tests/test_access_control.py::test_code_running_in_the_sandbox_cannot_read_protected_files` |
| All tool calls must be logged | The hub logs every call and retry; the tool loop logs refused calls | `tests/test_mcp_client.py::test_valid_and_invalid_calls_are_logged_and_redacted`, `tests/test_tool_contracts.py::test_rejected_model_calls_are_logged_and_never_reach_a_server`, `tests/test_tool_contracts.py::test_every_tool_has_a_valid_call_that_succeeds_and_is_logged` |

### Blocked actions

| Requirement | Control | Tests |
|---|---|---|
| Block `rm -rf` | Command policy | `tests/test_security.py::test_dangerous_commands_are_rejected`, `tests/test_sandbox_server.py::test_dangerous_commands_never_reach_the_runner` |
| Block `sudo` | Command policy; container runs without privileges | `tests/test_security.py::test_dangerous_commands_are_rejected`, `tests/test_sandbox_server.py::test_docker_command_has_isolation_flags` |
| Block `git push --force` | Command policy; the GitHub server never forces | `tests/test_github_publish.py::test_push_never_forces_a_diverged_branch`, `tests/test_command_policy.py::test_allowlist_bypass_attempts_are_rejected` |
| Block access outside the repository | Path guard | `tests/test_access_control.py::test_traversal_is_rejected_by_every_path_taking_tool`, `tests/test_filesystem_server.py::test_unauthorized_reads_are_errors` |
| Block reading .env files | Path guard; sandbox copy | `tests/test_access_control.py::test_case_variants_cannot_read_protected_files`, `tests/test_access_control.py::test_sandbox_copy_leaves_protected_files_and_outside_links_out` |
| Block reading private SSH keys | Path guard | `tests/test_security.py::test_forbidden_files_are_rejected`, `tests/test_access_control.py::test_symlinks_to_protected_files_inside_the_repository_are_blocked` |
| Block unrestricted network requests | Test container without network; docs URL allowlist and private-address check | `tests/test_sandbox_server.py::test_docker_command_has_isolation_flags`, `tests/test_web_guard.py::test_rejected_urls_say_why`, `tests/test_web_guard.py::test_private_addresses_are_blocked` |
| Block unlimited test execution | Sandbox timeout and resource limits; hub deadline | `tests/test_sandbox_server.py::test_docker_runner_timeout_kills_container`, `tests/test_test_execution.py::test_configured_time_limits_reach_the_sandbox_and_the_hub` |

### Required controls

| Control | Tests |
|---|---|
| Path validation | `tests/test_security.py::test_path_traversal_is_rejected`, `tests/test_access_control.py::test_traversal_is_rejected_by_every_path_taking_tool` |
| Command allowlisting | `tests/test_security.py::test_allowlisted_commands_are_accepted`, `tests/test_command_policy.py::test_no_rejected_command_reaches_the_sandbox_runner` |
| Timeouts | `tests/test_hub_reliability.py::test_tool_timeout_is_clear_and_the_session_keeps_working`, `tests/test_test_execution.py::test_a_sandbox_that_never_answers_hits_the_hub_deadline_and_is_not_repeated` |
| Docker isolation | `tests/test_sandbox_server.py::test_docker_command_has_isolation_flags`, `tests/test_sandbox_server.py::test_docker_runner_uses_a_copy_and_reports_output` |
| Resource limits | `tests/test_sandbox_server.py::test_docker_command_has_isolation_flags`, `tests/test_reliability_workflow.py::test_run_tool_call_limit`, `tests/test_reliability_workflow.py::test_cost_limit_stops_the_run_before_the_next_model_call` |
| Secret redaction | `tests/test_security.py::test_redaction`, `tests/test_anthropic_provider.py::test_logs_contain_metadata_but_no_key_or_source` |
| Audit logs | `tests/test_mcp_client.py::test_valid_and_invalid_calls_are_logged_and_redacted`, `tests/test_reliability_workflow.py::test_timeline_and_usage_are_recorded_per_step` |

### Model configuration

| Requirement | Control | Tests |
|---|---|---|
| Use the official Anthropic SDK and the Messages API | `AnthropicProvider` | `tests/test_anthropic_provider.py::test_chat_sends_tools_and_parses_tool_calls` |
| Do not hardcode the API key or model name inside Python files | Both come only from `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`; a run without them fails clearly | `tests/test_anthropic_provider.py::test_model_comes_from_environment`, `tests/test_anthropic_provider.py::test_missing_api_key_fails_before_any_request`, `tests/test_docs.py::test_no_model_name_or_key_in_python_code` |
| Log request metadata without API keys or source code | Model-call log has metadata only | `tests/test_anthropic_provider.py::test_logs_contain_metadata_but_no_key_or_source` |

### GitHub workflow

| Requirement | Control | Tests |
|---|---|---|
| Branch creation requires approval | Publication checkpoint; the model has no GitHub write tools | `tests/test_publication_workflow.py::test_checkpoint_shows_every_action_and_publishes_on_approval`, `tests/test_publication_workflow.py::test_reject_with_feedback_redrafts_then_decline` |
| Pushing code requires approval | Publication checkpoint with an explicit level | `tests/test_publication_workflow.py::test_partial_publication` |
| Pull-request creation requires approval | Publication checkpoint | `tests/test_pull_requests.py::test_api_mode_pull_request_end_to_end`, `tests/test_publication_workflow.py::test_partial_publication` |
| Never use force push | Plain refspecs; diverged branches fail with a clear error | `tests/test_github_publish.py::test_push_never_forces_a_diverged_branch`, `tests/test_github_publish.py::test_api_push_keeps_token_out_of_argv_and_git_config` |
| Use least-privilege GitHub permissions | Fine-grained token for one repository (see [SETUP.md](SETUP.md#using-a-real-github-repository)); only the GitHub server gets it | `tests/test_mcp_client.py::test_stdio_configs_launch_all_servers_with_minimal_env`, `tests/test_hub_reliability.py::test_github_write_tools_are_never_retry_candidates` |
| Handle GitHub API errors clearly | Status-specific messages, temporary errors marked, token never in messages | `tests/test_github_server.py::test_api_failures_have_clear_messages`, `tests/test_github_failures.py::test_unusual_responses_become_clear_errors`, `tests/test_github_failures.py::test_github_outage_fails_the_run_cleanly_and_a_retry_continues` |

### Documentation reader

| Requirement | Control | Tests |
|---|---|---|
| The browser tool must be restricted to documentation and research tasks | URL allowlist from `config/docs_sources.json`; only the sites' own search indexes | `tests/test_web_guard.py::test_rejected_urls_say_why`, `tests/test_docs_server.py::test_shipped_sources_file_is_valid` |
| It must not log in to websites or perform unrelated account actions | GET only; no cookies, credentials or forms; login and account paths refused | `tests/test_web_guard.py::test_fetch_page_with_get_only_and_no_credentials`, `tests/test_web_guard.py::test_cookies_are_never_stored_or_sent`, `tests/test_web_guard.py::test_credentials_in_environment_are_ignored`, `tests/test_docs_workflow.py::test_model_cannot_use_docs_tools_to_change_anything` |

## Weaknesses found by testing

Testing each rule through every way in, not just the module that implements it, found eight
holes. All are fixed and covered by tests (details in [TESTING.md](TESTING.md#problems-the-tests-found)):

- **Case variants.** Protected names in another letter case were readable on case-insensitive
  file systems.
- **Symlinks to protected files.** These were exposed through search and the git diff.
- **Secrets in the sandbox.** Protected files were copied into the sandbox.
- **pytest argument files.** These got around the option allowlist.
- **Line endings.** CRLF was lost when patching.
- **Half-applied patch sets.** A stale patch in a set left the earlier patches applied.
- **Unknown tool arguments.** These were silently ignored.
- **GitHub responses.** Some unexpected responses surfaced as exceptions.

## Residual risks

- **No authentication on the API and UI.** They listen on 127.0.0.1 only. Put an authenticating
  proxy in front before exposing them.
- **Docker socket in Docker Compose.** The API container can control the host's Docker daemon.
  The local install avoids this.
- **Test code runs.** Tests run the repository's own code, including patches the model wrote.
  The container limits what that code can reach, but a container escape in Docker itself is out
  of scope.
- **DNS rebinding.** The docs reader resolves a host before connecting, so a fast-changing DNS
  answer could in principle reach a private address. The URL allowlist is the main control.
- **Hard links.** The path guard sees symlinks but not hard links. A hard link placed in the
  repository by someone with access to it could expose a file elsewhere on the same disk. No
  tool can create links.
- **Human approval is the last line.** A reviewer who approves without reading can approve a
  harmful patch. The checkpoint shows exact diffs and the files outside the plan, but it cannot
  judge them.
- **Prompt injection can still waste budget or mislead the reviewer.** It cannot write or
  publish, but it can steer the proposal. Issue #43 in the fixtures contains an injection attempt
  for manual testing.

## Reporting

This is a portfolio project without a security contact. If you find a problem, open an issue
without exploit details and ask for a private channel.
