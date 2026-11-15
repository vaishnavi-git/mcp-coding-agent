# Run `20261115-033348-71a1fe`: issue #42

> Live run with claude-sonnet-5 on 2026-11-15 (UTC): the first end-to-end run with a real model. The patch and the publication were approved by hand in the UI. Tests ran in the Docker sandbox (Docker Desktop on macOS, Apple silicon). Fixture mode, so the push went to a local stand-in for GitHub and the pull request was saved locally. Cost cap: $1 per run. A single trial run, not the evaluation experiment.

| Status | Model | Started | Last update | Model calls | Tool calls | Cost | Agent time | Reviewer wait |
|---|---|---|---|---|---|---|---|---|
| published | `claude-sonnet-5` | 2026-11-15T03:33:48.110+00:00 | 2026-11-15T03:38:57.451+00:00 | 8 | 22 | $0.0938 | 51.9 s | 4.2 min |

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

SessionStore.refresh() does not properly rotate refresh tokens: the newly issued refresh token cannot be used for a subsequent refresh (raises InvalidToken), while the original, already-used refresh token remains valid indefinitely. Refresh tokens should be single-use, and each refresh() call should return a refresh token that works for the next refresh.

1. Locate and read SessionStore.refresh and related token storage/validation logic in authkit/sessions.py
2. Trace how refresh tokens are stored, looked up, and invalidated during login() and refresh() to understand the current token bookkeeping (e.g., mapping keyed by session id vs token value, stale token references)
3. Identify the bug: likely the new refresh token is not persisted/indexed correctly (e.g., old token not removed from valid set, or new token not added, or session lookup keyed incorrectly causing the new token to be rejected)
4. Fix refresh() so that: (a) the old refresh token is invalidated/removed immediately upon use, (b) a new refresh token is generated and correctly stored/indexed so it can be validated on the next refresh call
5. Review InvalidToken raising logic to ensure it only triggers for truly invalid/used tokens, not newly issued ones
6. Add/update regression test tests/test_sessions.py::test_refresh_rotates_refresh_token to cover: refresh once, refresh again with new token succeeds, and reusing the original token after rotation raises InvalidToken
7. Run the full test suite (pytest tests/test_sessions.py and full suite) to confirm the fix and no regressions
8. Review diff to ensure minimal, targeted change scoped to refresh/token rotation logic

Acceptance criteria:

- store.refresh(s1.refresh_token) followed by store.refresh(s2.refresh_token) succeeds without raising InvalidToken
- After s2 = store.refresh(s1.refresh_token), calling store.refresh(s1.refresh_token) again raises InvalidToken (old token is single-use and invalidated)
- tests/test_sessions.py::test_refresh_rotates_refresh_token passes
- Full existing test suite passes with no regressions
- No unrelated code or test changes introduced

Files expected to change: `authkit/sessions.py`, `tests/test_sessions.py`

## 3. Investigation

- search `def refresh(` (workflow): 1 matching line(s)
- search `class SessionStore` (workflow): 1 matching line(s)
- search `InvalidToken` (workflow): 11 matching line(s)
- search `refresh_token` (workflow): 13 matching line(s)
- search `def login(` (workflow): 1 matching line(s)
- files read: `authkit/sessions.py`, `tests/test_sessions.py`

Findings reported by the model:

> ## Findings
>
> **1. Root cause**
>
> File: `authkit/sessions.py`, method `SessionStore.refresh` (lines 56–64).
>
> ```python
> def refresh(self, refresh_token: str) -> Session:
>     """Exchange a refresh token for a new session. Refresh tokens are single-use."""
>     session = self._by_refresh.get(refresh_token)
>     if session is None or session.refresh_expires_at <= self._clock():
>         raise InvalidToken("refresh token is invalid or expired")
>     new_session = self._new_session(session.user_id)
>     self._by_access.pop(session.access_token, None)
>     self._by_access[new_session.access_token] = new_session
>     return new_session
> ```
>
> `refresh()` only updates the `_by_access` index (removing the old access token, adding the new one). It never touches `_by_refresh`:
> - The old refresh token is **never removed** from `self._by_refresh`, so `self._by_refresh[old_refresh_token]` still points at the stale `session` object — this is why `store.refresh(s1.refresh_token)` keeps working after it has already been used (refresh tokens are not actually single-use).
> - The newly created `new_session.refresh_token` (generated by `_new_session`) is **never inserted** into `self._by_refresh`. So when the caller later calls `store.refresh(s2.refresh_token)`, the lookup `self._by_refresh.get(refresh_token)` returns `None`, and `InvalidToken` is raised even though `s2.refresh_token` is a token the API just issued.
>
> So the bug is a missing pair of dictionary updates in `refresh()`: it needs to pop the just-used refresh token out of `_by_refresh` and register the new session's refresh token in `_by_refresh`, mirroring what `login()` does for both maps and what the access-token handling already does correctly.
>
> **2. Files that must change**
>
> - `authkit/sessions.py` — fix `SessionStore.refresh` to invalidate the old refresh-token entry (`self._by_refresh.pop(refresh_token, None)`) and register the new one (`self._by_refresh[new_session.refresh_token] = new_session`), keeping `_by_access` and `_by_refresh` consistent, just as `login()` populates both maps.
> - `tests/test_sessions.py` — the existing `test_refresh_rotates_refresh_token` only checks that reusing the *original* token fails after one refresh; it doesn't cover the "refresh twice in a row with the newly rotated token" scenario from the issue. Per the maintainer's request, this test (or a new one) should be extended to also assert that `store.refresh(s2.refresh_token)` succeeds (chained refresh) and that the then-stale `s1`/`s2` tokens are rejected, matching the reproduction steps in the issue.
>
> **3. Tests that should demonstrate the fix**
>
> - `tests/test_sessions.py::test_refresh_rotates_refresh_token` — needs to be expanded (as the maintainer requested) to perform a second refresh using the token returned by the first refresh, asserting it succeeds, and to assert that reusing `s1.refresh_token` again after rotation raises `InvalidToken`. This directly encodes the repro steps from the issue:
>   - `s2 = store.refresh(s1.refresh_token)` succeeds
>   - `s3 = store.refresh(s2.refresh_token)` succeeds (currently raises `InvalidToken` — the reported bug)
>   - `store.refresh(s1.refresh_token)` raises `InvalidToken` (currently succeeds — the other reported bug)
> - The pre-existing `test_refresh_issues_new_access_token` and `test_logout_revokes_tokens` should continue to pass and act as regression guards that the fix doesn't break access-token rotation or logout revocation.
>
> No documentation lookups were needed; the bug is fully evident from the in-memory dict bookkeeping in `sessions.py` compared with how `login()`/`logout()` consistently maintain both `_by_access` and `_by_refresh`.

## 4. Patch checkpoints

1. approved by human at 2026-11-15T03:36:35+00:00: `authkit/sessions.py`, `tests/test_sessions.py`

## 5. Applied changes

**`authkit/sessions.py`** (fix attempt 0, +2 / -0)

```diff
--- a/authkit/sessions.py
+++ b/authkit/sessions.py
@@ -59,6 +59,8 @@
         if session is None or session.refresh_expires_at <= self._clock():
             raise InvalidToken("refresh token is invalid or expired")
         new_session = self._new_session(session.user_id)
+        self._by_refresh.pop(refresh_token, None)
+        self._by_refresh[new_session.refresh_token] = new_session
         self._by_access.pop(session.access_token, None)
         self._by_access[new_session.access_token] = new_session
         return new_session
```

**`tests/test_sessions.py`** (fix attempt 0, +7 / -1)

```diff
--- a/tests/test_sessions.py
+++ b/tests/test_sessions.py
@@ -40,9 +40,15 @@
 def test_refresh_rotates_refresh_token():
     store = SessionStore()
     session = store.login("alice")
-    store.refresh(session.refresh_token)
+    refreshed_once = store.refresh(session.refresh_token)
+    # The newly issued refresh token must work for the next refresh.
+    refreshed_twice = store.refresh(refreshed_once.refresh_token)
+    assert store.authenticate(refreshed_twice.access_token) == "alice"
+    # Previously used refresh tokens must not work again.
     with pytest.raises(InvalidToken):
         store.refresh(session.refresh_token)  # a used refresh token must not work again
+    with pytest.raises(InvalidToken):
+        store.refresh(refreshed_once.refresh_token)
 
 
 def test_logout_revokes_tokens():
```

## 6. Test runs

| Attempt | Status | Summary | Failure kind | Failing tests |
|---|---|---|---|---|
| 0 | PASSED | 5 passed in 0.01s | passed | - |

## 7. Publication

Status: `pr_created`, branch `agent/issue-42-fix-refresh-token-rotation`

- `create_branch`: ok
- `commit_changes`: ok
- `push_branch`: ok
- `create_pull_request`: ok
- pull request: fixture://demo/sample-repo/pull/1 (draft) into `main`

Pull request **Fix refresh token rotation so tokens are single-use**:

```markdown
## Summary
Refreshing a session a second time raised InvalidToken because the newly issued refresh token was never added to the internal lookup table, while the previously used refresh token was never removed and remained valid indefinitely. The fix updates SessionStore.refresh() to pop the old refresh token from the lookup map and register the new session's refresh token in its place, making each refresh token single-use as documented. A regression test was added to cover refreshing a session twice in a row and to verify that both the original and the first-rotated refresh token are rejected after use.

## Changes
- `authkit/sessions.py` (+2 / -0)
- `tests/test_sessions.py` (+7 / -1)

## Tests
Run in the Docker sandbox: `pytest -q -p no:cacheprovider`
- Attempt 0: PASSED (5 passed in 0.01s)

## Risks
- Verify no other code paths rely on the old refresh token remaining valid after a refresh (e.g. concurrent refresh attempts or retry logic)
- Double-check that removing the old refresh token entry doesn't affect any logout or revocation logic that may also reference _by_refresh
- Confirm there are no other tests or callers that assume the previous refresh token continues to work after rotation

Fixes #42

---
_Generated by MCP Coding Agent. A human reviewer approved every code change before it was applied, and approved this pull request._
```

## 8. Timeline

| Step | Outcome | Duration | Model calls | Tool calls | Cost |
|---|---|---|---|---|---|
| `fetch_issue` | ok | 9 ms | 0 | 1 | $0.0000 |
| `plan` | ok | 10,093 ms | 1 | 0 | $0.0105 |
| `investigate` | ok | 13,932 ms | 2 | 7 | $0.0314 |
| `propose` | ok | 12,796 ms | 3 | 4 | $0.0381 |
| `apply` | ok | 49 ms | 0 | 5 | $0.0000 |
| `run_tests` | ok | 3,206 ms | 0 | 1 | $0.0000 |
| `draft_publication` | ok | 6,594 ms | 1 | 0 | $0.0085 |
| `publish` | ok | 283 ms | 0 | 4 | $0.0000 |
| `finalize` | ok | 4,987 ms | 1 | 0 | $0.0052 |

## 9. Tool calls (audit log)

| # | Step | By | Tool | Risk | Result | Duration | Attempt |
|---|---|---|---|---|---|---|---|
| 1 | fetch_issue | workflow | `get_github_issue` | read | ok | 9 ms | 1 |
| 2 | investigate | workflow | `search_repository` | read | ok | 85 ms | 1 |
| 3 | investigate | workflow | `search_repository` | read | ok | 2 ms | 1 |
| 4 | investigate | workflow | `search_repository` | read | ok | 1 ms | 1 |
| 5 | investigate | workflow | `search_repository` | read | ok | 1 ms | 1 |
| 6 | investigate | workflow | `search_repository` | read | ok | 1 ms | 1 |
| 7 | investigate | llm | `read_repository_file` | read | ok | 6 ms | 1 |
| 8 | investigate | llm | `read_repository_file` | read | ok | 1 ms | 1 |
| 9 | propose | llm | `read_repository_file` | read | ok | 33 ms | 1 |
| 10 | propose | llm | `read_repository_file` | read | ok | 2 ms | 1 |
| 11 | propose | llm | `propose_patch` | read | ok | 12 ms | 1 |
| 12 | propose | llm | `propose_patch` | read | ok | 2 ms | 1 |
| 13 | apply | workflow | `propose_patch` | read | ok | 2 ms | 1 |
| 14 | apply | workflow | `propose_patch` | read | ok | 1 ms | 1 |
| 15 | apply | workflow | `apply_patch` | write | ok | 1 ms | 1 |
| 16 | apply | workflow | `apply_patch` | write | ok | 1 ms | 1 |
| 17 | apply | workflow | `get_git_diff` | read | ok | 40 ms | 1 |
| 18 | run_tests | workflow | `run_tests` | read | ok | 3,203 ms | 1 |
| 19 | publish | workflow | `create_branch` | write | ok | 67 ms | 1 |
| 20 | publish | workflow | `commit_changes` | write | ok | 87 ms | 1 |
| 21 | publish | workflow | `push_branch` | external | ok | 93 ms | 1 |
| 22 | publish | workflow | `create_pull_request` | external | ok | 32 ms | 1 |

## 10. Model calls

| # | Purpose | Model | Latency | Input tokens | Output tokens | Cost | Error |
|---|---|---|---|---|---|---|---|
| 1 | plan | `claude-sonnet-5` | 10,092 ms | 1237 | 804 | $0.0105 | - |
| 2 | investigate | `claude-sonnet-5` | 1,441 ms | 2 | 114 | $0.0119 | - |
| 3 | investigate | `claude-sonnet-5` | 12,373 ms | 2 | 1352 | $0.0195 | - |
| 4 | propose | `claude-sonnet-5` | 1,717 ms | 2 | 134 | $0.0143 | - |
| 5 | propose | `claude-sonnet-5` | 5,703 ms | 2 | 724 | $0.0135 | - |
| 6 | propose | `claude-sonnet-5` | 5,316 ms | 2 | 498 | $0.0104 | - |
| 7 | draft_publication | `claude-sonnet-5` | 6,589 ms | 2027 | 443 | $0.0085 | - |
| 8 | summary | `claude-sonnet-5` | 4,984 ms | 576 | 409 | $0.0052 | - |

## 11. Final summary

> Summary: Fixed a refresh-token rotation bug in `SessionStore.refresh()` (authkit/sessions.py) where newly issued refresh tokens couldn't be used for subsequent refreshes (incorrectly raised `InvalidToken`), while old, already-used refresh tokens remained valid indefinitely instead of being invalidated. The fix makes refresh tokens properly single-use: each `refresh()` call now invalidates the token it consumed and issues a new, working refresh token for the next call. Corresponding tests were added/updated in tests/test_sessions.py, and the full suite passed (5 passed in 0.01s) on the first attempt.
>
> Changes were committed to branch `agent/issue-42-fix-refresh-token-rotation` (commit de29f2f) and a PR was opened (fixture://demo/sample-repo/pull/1). Before merging, a human should double check: (1) that old sessions/refresh tokens already issued in production won't be broken by the new rotation logic, (2) concurrency/race conditions if multiple refresh requests can arrive near-simultaneously with the same token, and (3) that any token storage/expiry cleanup is handled so invalidated tokens don't accumulate indefinitely.

