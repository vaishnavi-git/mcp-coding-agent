# Run `20261115-024850-10bb13`: issue #42

> Example run, produced by `scripts/make_example_run.py` and rendered by `scripts/render_run.py` on 2026-11-15. Everything except the model is real: the LangGraph workflow, the four MCP servers, git, the approval checkpoints (answered by the script), and pytest on a copy of the sample repository. The model's turns are **scripted** (an incomplete first patch, then the fix), so the plan, findings and patches are fixed inputs, not Claude's output. Token counts are zero and costs n/a because no model was called, and timings reflect a scripted model. Tests ran with a local pytest stand-in instead of Docker. This is not an evaluation result.

| Status | Model | Started | Last update | Model calls | Tool calls | Cost | Agent time | Reviewer wait |
|---|---|---|---|---|---|---|---|---|
| published | `scripted-model` | 2026-11-15T02:48:50.208+00:00 | 2026-11-15T02:48:52.017+00:00 | 10 | 22 | n/a | 1.6 s | 0.1 s |

## 1. Issue

> # Issue #42: Refreshing a session twice logs the user out; old refresh tokens stay valid
> repository: demo/sample-repo  state: open  labels: bug, auth
>
> ## Summary
> After a session is refreshed once, the next refresh fails with `InvalidToken` and the user is logged out. At the same time, the refresh token that was already used keeps working, so refresh tokens are not single-use as documented.
>
> ## Steps to reproduce
> ```python
> store = SessionStore()
> s1 = store.login("alice")
> s2 = store.refresh(s1.refresh_token)   # works
> s3 = store.refresh(s2.refresh_token)   # raises InvalidToken  <-- bug
> store.refresh(s1.refresh_token)        # still works          <-- bug
> ```
>
> ## Expected
> - The refresh token returned by `refresh()` can be used for the next refresh.
> - A refresh token can be used only once.
>
> ## Actual
> CI is red: `tests/test_sessions.py::test_refresh_rotates_refresh_token` fails.
>
> ## Comments (1)
> - maintainer: Probably in `SessionStore.refresh` in `authkit/sessions.py`. Please add a regression test for refreshing twice in a row.

## 2. Plan

SessionStore.refresh does not rotate refresh tokens.

1. Read SessionStore.refresh
2. Register the new refresh token and revoke the used one
3. Add a regression test
4. Run the test suite

Acceptance criteria:

- A used refresh token is rejected
- The new refresh token can be used

Files expected to change: `authkit/sessions.py`, `tests/test_sessions.py`

## 3. Investigation

- search `def refresh` (workflow): 1 matching line(s)
- search `_by_refresh` (workflow): 4 matching line(s)
- files read: `authkit/sessions.py`, `tests/test_sessions.py`

Findings reported by the model:

> Root cause: SessionStore.refresh (authkit/sessions.py) never stores the new refresh token and never revokes the used one.

## 4. Patch checkpoints

1. approved by human at 2026-11-15T02:48:50+00:00: `authkit/sessions.py`
2. approved by human at 2026-11-15T02:48:51+00:00: `authkit/sessions.py`, `tests/test_sessions.py`

## 5. Applied changes

**`authkit/sessions.py`** (fix attempt 0, +1 / -0)

```diff
--- a/authkit/sessions.py
+++ b/authkit/sessions.py
@@ -61,6 +61,7 @@
         new_session = self._new_session(session.user_id)
         self._by_access.pop(session.access_token, None)
         self._by_access[new_session.access_token] = new_session
+        self._by_refresh[new_session.refresh_token] = new_session
         return new_session
 
     def logout(self, access_token: str) -> None:
```

**`authkit/sessions.py`** (fix attempt 1, +1 / -0)

```diff
--- a/authkit/sessions.py
+++ b/authkit/sessions.py
@@ -60,6 +60,7 @@
             raise InvalidToken("refresh token is invalid or expired")
         new_session = self._new_session(session.user_id)
         self._by_access.pop(session.access_token, None)
+        self._by_refresh.pop(refresh_token, None)
         self._by_access[new_session.access_token] = new_session
         self._by_refresh[new_session.refresh_token] = new_session
         return new_session
```

**`tests/test_sessions.py`** (fix attempt 1, +8 / -0)

```diff
--- a/tests/test_sessions.py
+++ b/tests/test_sessions.py
@@ -53,3 +53,11 @@
         store.authenticate(session.access_token)
     with pytest.raises(InvalidToken):
         store.refresh(session.refresh_token)
+
+
+def test_refresh_twice_in_a_row():
+    store = SessionStore()
+    first = store.login("alice")
+    second = store.refresh(first.refresh_token)
+    third = store.refresh(second.refresh_token)
+    assert store.authenticate(third.access_token) == "alice"
```

## 6. Test runs

| Attempt | Status | Summary | Failure kind | Failing tests |
|---|---|---|---|---|
| 0 | FAILED | 1 failed, 4 passed in 0.03s | test_failures | `tests/test_sessions.py::test_refresh_rotates_refresh_token` |
| 1 | PASSED | 6 passed in 0.01s | passed | - |

## 7. Publication

Status: `pr_created`, branch `agent/issue-42-rotate-refresh-tokens`

- `create_branch`: ok
- `commit_changes`: ok
- `push_branch`: ok
- `create_pull_request`: ok
- pull request: fixture://demo/sample-repo/pull/1 (draft) into `main`

Pull request **Rotate refresh tokens on refresh**:

```markdown
## Summary
Refreshing a session never stored the new refresh token and never revoked the used one. The fix does both.

## Changes
- `authkit/sessions.py` (+2 / -0)
- `tests/test_sessions.py` (+8 / -0)

## Tests
Run in the Docker sandbox: `pytest -q -p no:cacheprovider`
- Attempt 0: FAILED (1 failed, 4 passed in 0.03s)
- Attempt 1: PASSED (6 passed in 0.01s)

## Risks
- Clients that reuse an old refresh token will now be logged out.

Fixes #42

---
_Generated by MCP Coding Agent. A human reviewer approved every code change before it was applied, and approved this pull request._
```

## 8. Timeline

| Step | Outcome | Duration | Model calls | Tool calls | Cost |
|---|---|---|---|---|---|
| `fetch_issue` | ok | 1 ms | 0 | 1 | $0.0000 |
| `plan` | ok | 0 ms | 1 | 0 | $0.0000 |
| `investigate` | ok | 6 ms | 3 | 4 | $0.0000 |
| `propose` | ok | 1 ms | 2 | 1 | $0.0000 |
| `apply` | ok | 39 ms | 0 | 3 | $0.0000 |
| `run_tests` | ok | 679 ms | 0 | 1 | $0.0000 |
| `propose` | ok | 3 ms | 2 | 2 | $0.0000 |
| `apply` | ok | 38 ms | 0 | 5 | $0.0000 |
| `run_tests` | ok | 610 ms | 0 | 1 | $0.0000 |
| `draft_publication` | ok | 0 ms | 1 | 0 | $0.0000 |
| `publish` | ok | 230 ms | 0 | 4 | $0.0000 |
| `finalize` | ok | 0 ms | 1 | 0 | $0.0000 |

## 9. Tool calls (audit log)

| # | Step | By | Tool | Risk | Result | Duration | Attempt |
|---|---|---|---|---|---|---|---|
| 1 | fetch_issue | workflow | `get_github_issue` | read | ok | 1 ms | 1 |
| 2 | investigate | workflow | `search_repository` | read | ok | 1 ms | 1 |
| 3 | investigate | workflow | `search_repository` | read | ok | 0 ms | 1 |
| 4 | investigate | llm | `read_repository_file` | read | ok | 0 ms | 1 |
| 5 | investigate | llm | `read_repository_file` | read | ok | 0 ms | 1 |
| 6 | propose | llm | `propose_patch` | read | ok | 0 ms | 1 |
| 7 | apply | workflow | `propose_patch` | read | ok | 0 ms | 1 |
| 8 | apply | workflow | `apply_patch` | write | ok | 0 ms | 1 |
| 9 | apply | workflow | `get_git_diff` | read | ok | 36 ms | 1 |
| 10 | run_tests | workflow | `run_tests` | read | ok | 678 ms | 1 |
| 11 | propose | llm | `propose_patch` | read | ok | 1 ms | 1 |
| 12 | propose | llm | `propose_patch` | read | ok | 0 ms | 1 |
| 13 | apply | workflow | `propose_patch` | read | ok | 0 ms | 1 |
| 14 | apply | workflow | `propose_patch` | read | ok | 0 ms | 1 |
| 15 | apply | workflow | `apply_patch` | write | ok | 0 ms | 1 |
| 16 | apply | workflow | `apply_patch` | write | ok | 0 ms | 1 |
| 17 | apply | workflow | `get_git_diff` | read | ok | 33 ms | 1 |
| 18 | run_tests | workflow | `run_tests` | read | ok | 609 ms | 1 |
| 19 | publish | workflow | `create_branch` | write | ok | 43 ms | 1 |
| 20 | publish | workflow | `commit_changes` | write | ok | 74 ms | 1 |
| 21 | publish | workflow | `push_branch` | external | ok | 76 ms | 1 |
| 22 | publish | workflow | `create_pull_request` | external | ok | 28 ms | 1 |

## 10. Model calls

| # | Purpose | Model | Latency | Input tokens | Output tokens | Cost | Error |
|---|---|---|---|---|---|---|---|
| 1 | plan | `scripted-model` | 1 ms | 0 | 0 | n/a | - |
| 2 | investigate | `scripted-model` | 1 ms | 0 | 0 | n/a | - |
| 3 | investigate | `scripted-model` | 1 ms | 0 | 0 | n/a | - |
| 4 | investigate | `scripted-model` | 1 ms | 0 | 0 | n/a | - |
| 5 | propose | `scripted-model` | 1 ms | 0 | 0 | n/a | - |
| 6 | propose | `scripted-model` | 1 ms | 0 | 0 | n/a | - |
| 7 | propose | `scripted-model` | 1 ms | 0 | 0 | n/a | - |
| 8 | propose | `scripted-model` | 1 ms | 0 | 0 | n/a | - |
| 9 | draft_publication | `scripted-model` | 1 ms | 0 | 0 | n/a | - |
| 10 | summary | `scripted-model` | 1 ms | 0 | 0 | n/a | - |

## 11. Final summary

> Refresh tokens are now rotated: refresh() stores the new refresh token and revokes the used one. A regression test covers two refreshes in a row; all tests pass.

