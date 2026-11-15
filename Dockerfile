# App image: the API (FastAPI) and the UI (Streamlit). The MCP servers run as
# subprocesses of the API, inside this container.
#
# Tests never run in this image. The sandbox server starts a separate, locked-down
# container from the test image (sandbox/Dockerfile) through the Docker daemon, so
# this image contains the Docker CLI and the compose file gives it the daemon socket.
#
# Build:  docker build -t mcp-coding-agent:latest .
#         docker build --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" -t mcp-coding-agent:latest .
# (Match your user ID so the container can use git on repositories you mount.)
# No secrets are baked in: settings come from .env at run time (see docker-compose.yml).

FROM docker:27.3.1-cli AS docker-cli

FROM python:3.11-slim

ARG UID=1000
ARG GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${GID}" agent \
    && useradd --create-home --uid "${UID}" --gid "${GID}" agent

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY config ./config
COPY examples ./examples
COPY scripts ./scripts
# Editable install: the app finds config/ and examples/ relative to /app.
RUN pip install -e ".[ui]" \
    && mkdir -p /app/data /app/workspaces \
    && chown agent:agent /app/data /app/workspaces

USER agent
EXPOSE 8000 8501

# Listens on all interfaces inside the container; docker-compose.yml publishes the
# ports on 127.0.0.1 only. The API has no authentication.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
