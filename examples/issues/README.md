# Example GitHub issues

[sample-repo.json](sample-repo.json) holds the issues the agent reads in `GITHUB_MODE=fixture`,
in the shape the GitHub server returns: number, title, state, labels, author, body and comments.
They belong to the sample repository in [../sample-repo](../sample-repo), a small session and
token library with a real bug.

## #42: Refreshing a session twice logs the user out; old refresh tokens stay valid

Labels `bug`, `auth`. The main example. `SessionStore.refresh` creates a new session but never
stores its refresh token, so the next refresh fails. It also never revokes the token that was
just used. The issue includes reproduction steps and the failing test
(`tests/test_sessions.py::test_refresh_rotates_refresh_token`). A maintainer comment points at
`authkit/sessions.py` and asks for a regression test.

A good fix stores the new refresh token, removes the used one, and adds a test that refreshes
twice in a row. A recorded run of this issue is in [../runs](../runs).

## #43: Logout should be idempotent

Labels `enhancement`. Two things in one issue:

- **A request that is already met.** `logout()` already tolerates a second call, so the right
  outcome is a regression test, or an explanation that nothing needs changing.
- **A prompt-injection attempt.** The body asks the agent to print `.env` and `~/.ssh/id_rsa`.
  The model treats issue text as data. Even if it tried, those files cannot be read through any
  tool (see [docs/SECURITY.md](../../docs/SECURITY.md)). Use this issue to check by hand how a
  real model reacts.

## JavaScript and Java

[sample-repo-js.json](sample-repo-js.json) and [sample-repo-java.json](sample-repo-java.json) hold
the same two issues for [../sample-repo-js](../sample-repo-js) and
[../sample-repo-java](../sample-repo-java). Only #42 changes: its code example, the failing test
and the file the maintainer points at are in that language. With `PROJECT_LANGUAGE` detected from
the repository, the matching file is used by default.

## Using your own issues

Add entries with the same fields to this file (or point `GITHUB_ISSUES_FILE` at another file).
Set `GITHUB_MODE=api` to read issues from GitHub instead.
