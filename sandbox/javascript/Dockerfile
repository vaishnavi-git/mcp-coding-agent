# Test-runner image for JavaScript projects that use Node's built-in test runner.
# Build:  docker build -t mcp-agent-sandbox-js:latest sandbox/javascript/
#
# Tests run with no network, so `npm install` can't happen in the sandbox. If the target
# repository has dependencies, extend this image: install them under /opt/node_modules and set
# NODE_PATH=/opt/node_modules.
FROM node:22-slim

ENV NODE_ENV=test

USER node
WORKDIR /workspace
