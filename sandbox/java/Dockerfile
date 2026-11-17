# Test-runner image for Java projects built with Maven and tested with JUnit 5.
# Build:  docker build -t mcp-agent-sandbox-java:latest sandbox/java/
#
# Tests run with no network, so Maven has to find everything in the image. The build runs
# `mvn test` once on a small project (prefetch/) with the same plugin and JUnit versions as
# examples/sample-repo-java, which leaves all of them in /opt/m2. At test time that repository is
# read-only: Maven reads it as a "tail" behind a scratch repository in /tmp. If the target
# repository needs other dependencies or plugin versions, add them to prefetch/pom.xml.
FROM maven:3.9.9-eclipse-temurin-21

COPY prefetch/ /tmp/prefetch/
RUN mvn -B -q -ntp -Dmaven.repo.local=/opt/m2 -f /tmp/prefetch/pom.xml test \
    && rm -rf /tmp/prefetch \
    && chmod -R a+rX /opt/m2 \
    && useradd --create-home --uid 10001 runner

# Maven's console library loads a native helper. Only the Windows builds ship unpacked, so on Linux
# it would unpack one into /tmp, which the sandbox mounts noexec, and print an error on every run.
RUN cd /tmp && jar xf /usr/share/maven/lib/jansi-*.jar org/fusesource/jansi/internal/native/Linux \
    && cp -r org/fusesource/jansi/internal/native/Linux /usr/share/maven/lib/jansi-native/ \
    && rm -rf /tmp/org

ENV MAVEN_ARGS="-Dmaven.repo.local=/tmp/m2 -Dmaven.repo.local.tail=/opt/m2 -ntp" \
    MAVEN_OPTS="-Xmx384m"

# The base image's entrypoint copies settings into the home directory, which the sandbox's
# read-only root filesystem doesn't allow, and nothing here needs it.
ENTRYPOINT []
USER runner
WORKDIR /workspace
