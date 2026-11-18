"""Which tests cover each test category.

``conftest.py`` turns this mapping into pytest markers, so one category can be
run on its own (``pytest -m path_traversal``), and every run ends with a table
of results per category. ``test_spec_matrix.py`` fails if a listed test no
longer exists, if a category has too few tests, or if a test in one of the
category files is not listed anywhere.

Entries are ``<file>::<test function>``; every parametrized case of the
function belongs to the category. A test can be in more than one category.
"""

from __future__ import annotations

SPEC_CATEGORIES: dict[str, str] = {
    "discovery": "Tool discovery",
    "valid_calls": "Valid tool calls",
    "invalid_args": "Invalid arguments",
    "path_traversal": "Path traversal attempts",
    "unauthorized_access": "Unauthorized file access",
    "dangerous_commands": "Dangerous command rejection",
    "timeouts": "Test timeout handling",
    "recovery": "Failed test recovery",
    "patching": "Patch application",
    "github_failures": "GitHub API failures",
    "pull_requests": "Pull-request creation",
}

# Test files written around these categories; every test in them must be in at least one category.
CATEGORY_FILES = (
    "test_tool_contracts.py",
    "test_access_control.py",
    "test_command_policy.py",
    "test_test_execution.py",
    "test_patch_application.py",
    "test_github_failures.py",
    "test_pull_requests.py",
)

MIN_TESTS_PER_CATEGORY = 5

SPEC_MATRIX: dict[str, list[str]] = {
    "discovery": [
        "test_tool_contracts.py::test_tool_catalog_matches_the_reviewed_contract",
        "test_tool_contracts.py::test_production_stdio_servers_expose_the_same_contract",
        "test_tool_contracts.py::test_duplicate_tool_names_stop_startup",
        "test_tool_contracts.py::test_unannotated_tools_are_treated_as_destructive",
        "test_mcp_client.py::test_classify_risk",
        "test_mcp_client.py::test_discovery_across_servers",
        "test_mcp_client.py::test_real_stdio_server_connection",
        "test_mcp_client.py::test_stdio_configs_launch_all_servers_with_minimal_env",
        "test_mcp_client.py::test_failing_server_is_reported_by_name",
        "test_filesystem_server.py::test_discovery_and_annotations",
        "test_docs_server.py::test_tools_are_discovered_as_read_only",
        "test_github_publish.py::test_tool_annotations",
        "test_agent_workflow.py::test_write_tools_are_never_offered_to_the_model",
        "test_api.py::test_full_run_through_the_api",
    ],
    "valid_calls": [
        "test_tool_contracts.py::test_every_tool_has_a_valid_call_that_succeeds_and_is_logged",
        "test_mcp_client.py::test_valid_and_invalid_calls_are_logged_and_redacted",
        "test_mcp_client.py::test_real_stdio_server_connection",
        "test_filesystem_server.py::test_search_and_read",
        "test_docs_server.py::test_search_and_read",
        "test_github_server.py::test_fixture_issue",
        "test_github_server.py::test_api_success_sends_token_and_reads_comments",
        "test_sandbox_server.py::test_run_tests_uses_default_command",
        "test_github_publish.py::test_create_branch_and_duplicates",
    ],
    "invalid_args": [
        "test_tool_contracts.py::test_every_tool_rejects_missing_wrong_type_and_unknown_arguments",
        "test_tool_contracts.py::test_out_of_range_values_get_a_clear_error",
        "test_tool_contracts.py::test_rejected_model_calls_are_logged_and_never_reach_a_server",
        "test_tool_contracts.py::test_size_and_result_limits_are_enforced",
        "test_agent_workflow.py::test_tool_loop_rejects_unknown_tools_bad_arguments_and_enforces_budget",
        "test_mcp_client.py::test_valid_and_invalid_calls_are_logged_and_redacted",
        "test_filesystem_server.py::test_invalid_arguments",
        "test_github_publish.py::test_invalid_branch_names",
        "test_api.py::test_validation_and_unknown_runs",
    ],
    "path_traversal": [
        "test_access_control.py::test_traversal_is_rejected_by_every_path_taking_tool",
        "test_access_control.py::test_odd_spellings_stay_inside_the_repository",
        "test_access_control.py::test_git_diff_and_commits_leave_protected_files_out",
        "test_security.py::test_path_traversal_is_rejected",
        "test_security.py::test_symlink_escape_is_rejected",
        "test_filesystem_server.py::test_unauthorized_reads_are_errors",
        "test_filesystem_server.py::test_symlink_escape_via_tool",
        "test_web_guard.py::test_rejected_urls_say_why",
    ],
    "unauthorized_access": [
        "test_access_control.py::test_protected_names_are_matched_in_any_letter_case",
        "test_access_control.py::test_case_variants_cannot_read_protected_files",
        "test_access_control.py::test_symlinks_to_protected_files_inside_the_repository_are_blocked",
        "test_access_control.py::test_search_never_returns_protected_content",
        "test_access_control.py::test_patches_cannot_create_or_change_protected_files",
        "test_access_control.py::test_git_diff_and_commits_leave_protected_files_out",
        "test_access_control.py::test_sandbox_copy_leaves_protected_files_and_outside_links_out",
        "test_access_control.py::test_hard_links_to_files_outside_the_repository_are_blocked",
        "test_access_control.py::test_code_running_in_the_sandbox_cannot_read_protected_files",
        "test_security.py::test_forbidden_files_are_rejected",
        "test_security.py::test_writable_paths_are_enforced",
        "test_security.py::test_walk_skips_forbidden_files",
        "test_security.py::test_redaction",
        "test_filesystem_server.py::test_unauthorized_reads_are_errors",
        "test_github_publish.py::test_commit_rules_and_file_filtering",
        "test_anthropic_provider.py::test_logs_contain_metadata_but_no_key_or_source",
        "test_mcp_client.py::test_stdio_configs_launch_all_servers_with_minimal_env",
        "test_docs_workflow.py::test_model_cannot_use_docs_tools_to_change_anything",
        "test_web_guard.py::test_private_addresses_are_blocked",
        "test_web_guard.py::test_credentials_in_environment_are_ignored",
    ],
    "dangerous_commands": [
        "test_command_policy.py::test_allowlist_bypass_attempts_are_rejected",
        "test_command_policy.py::test_ordinary_test_commands_still_work",
        "test_command_policy.py::test_no_rejected_command_reaches_the_sandbox_runner",
        "test_command_policy.py::test_pytest_really_expands_argument_files",
        "test_command_policy.py::test_the_model_cannot_choose_the_test_command",
        "test_command_policy.py::test_a_dangerous_configured_test_command_is_rejected_too",
        "test_command_policy.py::test_node_bypass_attempts_are_rejected",
        "test_command_policy.py::test_node_must_run_its_test_runner_even_with_a_wider_allowlist",
        "test_command_policy.py::test_maven_bypass_attempts_are_rejected",
        "test_command_policy.py::test_ordinary_node_test_commands_still_work",
        "test_command_policy.py::test_ordinary_maven_test_commands_still_work",
        "test_command_policy.py::test_no_rejected_node_or_maven_command_reaches_the_sandbox_runner",
        "test_security.py::test_dangerous_commands_are_rejected",
        "test_security.py::test_allowlisted_commands_are_accepted",
        "test_sandbox_server.py::test_dangerous_commands_never_reach_the_runner",
        "test_sandbox_server.py::test_docker_command_has_isolation_flags",
        "test_github_publish.py::test_push_never_forces_a_diverged_branch",
    ],
    "timeouts": [
        "test_test_execution.py::test_a_test_run_killed_at_the_time_limit_is_reported_as_a_timeout",
        "test_test_execution.py::test_a_sandbox_that_never_answers_hits_the_hub_deadline_and_is_not_repeated",
        "test_test_execution.py::test_configured_time_limits_reach_the_sandbox_and_the_hub",
        "test_sandbox_server.py::test_failed_and_timed_out_results_are_reported",
        "test_sandbox_server.py::test_docker_runner_timeout_kills_container",
        "test_hub_reliability.py::test_tool_timeout_is_clear_and_the_session_keeps_working",
        "test_hub_reliability.py::test_timeouts_are_retried_where_the_server_allows_it",
    ],
    "recovery": [
        "test_test_execution.py::test_a_test_run_killed_at_the_time_limit_is_reported_as_a_timeout",
        "test_test_execution.py::test_a_patch_that_breaks_collection_is_recognised_and_repaired",
        "test_test_execution.py::test_a_rejected_fix_is_not_applied",
        "test_agent_workflow.py::test_issue_to_passing_tests_with_failed_test_recovery",
        "test_agent_workflow.py::test_fix_attempts_are_limited",
        "test_agent_workflow.py::test_docker_unavailable_fails_after_apply",
        "test_reliability_workflow.py::test_failed_tests_are_analyzed_and_the_reviewer_can_grant_another_attempt",
        "test_reliability_workflow.py::test_tests_can_be_rerun_once_docker_is_back",
        "test_reliability_units.py::test_failing_tests_and_kind_are_extracted",
        "test_reliability_units.py::test_progress_between_attempts_is_described",
    ],
    "patching": [
        "test_patch_application.py::test_crlf_files_keep_crlf",
        "test_patch_application.py::test_context_lines_keep_their_original_text",
        "test_patch_application.py::test_a_file_without_a_final_newline_keeps_it_that_way",
        "test_patch_application.py::test_a_patch_that_no_longer_applies_writes_nothing",
        "test_patch_application.py::test_invalid_patches_are_rejected_and_write_nothing",
        "test_patch_application.py::test_a_stale_patch_stops_the_whole_set_before_anything_is_written",
        "test_patching.py::test_apply_simple_patch",
        "test_patching.py::test_wrong_line_numbers_are_tolerated_but_context_must_match",
        "test_patching.py::test_context_mismatch_is_rejected",
        "test_patching.py::test_new_file_patch",
        "test_patching.py::test_multiple_hunks_and_header_less_patch",
        "test_patching.py::test_malformed_patches_are_rejected",
        "test_patching.py::test_no_trailing_newline_is_preserved",
        "test_patching.py::test_unified_diff_and_stats",
        "test_filesystem_server.py::test_propose_does_not_write_and_apply_does",
        "test_filesystem_server.py::test_patch_errors",
        "test_filesystem_server.py::test_create_new_file_and_diff_includes_it",
        "test_reliability_workflow.py::test_a_failure_while_applying_is_not_retried_automatically",
    ],
    "github_failures": [
        "test_github_failures.py::test_unusual_responses_become_clear_errors",
        "test_github_failures.py::test_secondary_rate_limits_are_waited_out",
        "test_github_failures.py::test_pull_request_failures_are_never_retried",
        "test_github_failures.py::test_github_outage_fails_the_run_cleanly_and_a_retry_continues",
        "test_github_server.py::test_api_failures_have_clear_messages",
        "test_github_server.py::test_api_network_failures",
        "test_github_publish.py::test_api_pull_request_failures",
        "test_github_publish.py::test_api_pull_request_network_failure",
        "test_github_publish.py::test_api_push_requires_token",
        "test_github_publish.py::test_push_error_messages",
        "test_github_publish.py::test_missing_origin_is_a_clear_error",
        "test_hub_reliability.py::test_github_server_errors_are_retried_only_when_temporary",
        "test_hub_reliability.py::test_github_write_tools_are_never_retry_candidates",
        "test_agent_workflow.py::test_unknown_issue_fails_before_planning",
    ],
    "pull_requests": [
        "test_pull_requests.py::test_api_mode_pull_request_end_to_end",
        "test_pull_requests.py::test_a_pull_request_rejected_after_the_push_reports_what_was_done",
        "test_pull_requests.py::test_a_second_pull_request_for_the_same_branch_is_refused",
        "test_pull_requests.py::test_no_pull_request_from_the_default_branch_or_an_unpushed_branch",
        "test_github_publish.py::test_push_and_pull_request_in_fixture_mode",
        "test_github_publish.py::test_api_pull_request_request_shape",
        "test_github_publish.py::test_api_push_keeps_token_out_of_argv_and_git_config",
        "test_publication_workflow.py::test_checkpoint_shows_every_action_and_publishes_on_approval",
        "test_publication_workflow.py::test_partial_publication",
        "test_publication_workflow.py::test_reviewer_edits_are_used_and_recorded",
        "test_publication_workflow.py::test_invalid_edited_branch_fails_before_anything_is_published",
        "test_publication_workflow.py::test_reject_with_feedback_redrafts_then_decline",
        "test_publication_workflow.py::test_failure_midway_records_what_was_done",
        "test_publication_workflow.py::test_branch_names_and_pr_body_helpers",
    ],
}


def marker_index() -> dict[str, set[str]]:
    """``<file>::<function>`` -> the category markers it carries."""
    index: dict[str, set[str]] = {}
    for marker, tests in SPEC_MATRIX.items():
        for test in tests:
            index.setdefault(test, set()).add(marker)
    return index
