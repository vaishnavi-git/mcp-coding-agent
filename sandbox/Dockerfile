# Test-runner image used by the sandbox MCP server.
# Build:  docker build -t mcp-agent-sandbox:latest sandbox/
# Extend this image with your target repository's test dependencies.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install "pytest>=8,<10" \
    && useradd --create-home --uid 1000 runner

USER runner
WORKDIR /workspace
