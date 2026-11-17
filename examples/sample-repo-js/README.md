# authkit (JavaScript sample)

The JavaScript version of the sample target repository: the same small in-memory session library
as [../sample-repo](../sample-repo), with the same bug (issue #42 in
`examples/issues/sample-repo-js.json`). Refresh tokens aren't rotated, so a used refresh token keeps
working and the new one is never accepted.

It has no dependencies and uses Node's built-in test runner (Node 20 or later):

```bash
node --test
```
