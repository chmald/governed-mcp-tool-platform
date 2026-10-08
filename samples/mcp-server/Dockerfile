# syntax=docker/dockerfile:1
#
# Minimal, hardened container image for samples/mcp-server/server.py.
#
# server.py is standard-library only (see its module docstring), so there is
# no `pip install` step and therefore no pip cache to clean up. The base
# image's own package manager is never invoked (no apt-get install), so there
# is no apt cache left behind either.

FROM python:3.12-slim

# UID/GID 1100 is deliberate, not arbitrary: it matches the pod security
# context the MCP Gateway control plane applies to adapter StatefulSets
# (namespace `adapter`). Building the image to already run as this exact
# non-root UID/GID means it behaves identically whether it is smoke-tested
# standalone (`docker run`) or deployed as a registered MCP Gateway adapter.
RUN groupadd --gid 1100 mcpsample \
    && useradd --uid 1100 --gid 1100 --no-create-home --shell /usr/sbin/nologin mcpsample

WORKDIR /app

COPY server.py .

RUN chown -R 1100:1100 /app

USER 1100:1100

ENV PORT=8000
EXPOSE 8000

# Exec form avoids shell-quoting pitfalls; urlopen() raises on a non-2xx
# status or a connection failure, which is exactly the non-zero exit
# HEALTHCHECK needs to mark the container unhealthy.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/healthz', timeout=2)"]

ENTRYPOINT ["python", "server.py"]

# ---------------------------------------------------------------------------
# Build and publish to the ACR the MCP Gateway control plane is configured to
# pull from. Adapters must already exist in that registry -- the gateway does
# not build images itself, so this (or an equivalent CI step) always runs
# before `Register-McpAdapter.ps1`:
#
#   az acr build \
#     --registry <acr-name> \
#     --image mcp-samples/mcp-sample-server:1.0.0 \
#     --file samples/mcp-server/Dockerfile \
#     samples/mcp-server
# ---------------------------------------------------------------------------
