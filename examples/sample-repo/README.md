# authkit (sample target repository)

A tiny in-memory session library used as the target repository for the MCP
coding agent. It contains a real bug (see issue #42 in
`examples/issues/sample-repo.json`): refresh tokens are not rotated, so a
used refresh token keeps working and the new one is never accepted.

```bash
pytest -q
```
