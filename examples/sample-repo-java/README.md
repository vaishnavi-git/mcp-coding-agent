# authkit (Java sample)

The Java version of the sample target repository: the same small in-memory session library as
[../sample-repo](../sample-repo), with the same bug (issue #42 in
`examples/issues/sample-repo-java.json`). Refresh tokens aren't rotated, so a used refresh token
keeps working and the new one is never accepted.

Java 21, Maven and JUnit 5. In the agent's sandbox the tests run offline with the plugins baked into
the image (`sandbox/java/`):

```bash
mvn -o -B test
```
