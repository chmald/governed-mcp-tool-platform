# Sample MCP Server

A minimal, dependency-light **Streamable HTTP** MCP server used as the demo
payload for `secure-mcp-gateway-landing-zone`. It exists to be registered as
an **adapter** on MCP Gateway and called through the governance plane (Azure
API Management in front of it), so you can prove the whole pattern works
end-to-end without needing a real workload.

The server is standard-library-only Python (`http.server.ThreadingHTTPServer`)
-- no third-party runtime dependencies, which keeps the container image small
and the demo easy to reason about.

## Tools

It exposes exactly two generic, domain-agnostic tools over JSON-RPC 2.0:

| Tool | Purpose |
|---|---|
| `echo` | Returns its `text` input unchanged. Proves a basic MCP tool round-trip works through both governance layers. |
| `describe_environment` | Returns non-sensitive runtime facts: hostname, pod name (`HOSTNAME` env var), Python version, current UTC timestamp, and any forwarded `X-Mcp-UserId` / `X-Mcp-Roles` identity headers it received. **This is the demo's money shot** -- it visibly proves which pod served the request and which identity was forwarded to it. |

## Run locally

```bash
cd samples/mcp-server
python server.py                 # listens on 0.0.0.0:8000 (override with PORT)
```

Health check:

```bash
curl http://localhost:8000/healthz
```

Minimal handshake:

```bash
curl -s http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' -i
```

The response includes an `mcp-session-id` header -- that's what MCP Gateway's
session-affinity layer pins a caller to a replica with.

## Build and publish to your ACR

MCP Gateway does not build images -- it only pulls from a registry it's
already configured to trust. Build and push with `az acr build`:

```bash
az acr build \
  --registry <acr-name> \
  --image mcp-samples/mcp-sample-server:1.0.0 \
  --file samples/mcp-server/Dockerfile \
  samples/mcp-server
```

## Register as an MCP Gateway adapter

Use `scripts/Register-McpAdapter.ps1` once the image above exists in the
configured ACR:

```powershell
./scripts/Register-McpAdapter.ps1 `
  -GatewayBaseUrl 'https://<mcp-gateway-host>' `
  -Name 'mcp-sample-server' `
  -ImageName '<acr-name>.azurecr.io/mcp-samples/mcp-sample-server' `
  -ImageVersion '1.0.0' `
  -ReplicaCount 2 `
  -TenantId '<TENANT_ID>' `
  -ClientId '<CLIENT_ID>'
```

The script prints the resulting client URL:
`{GatewayBaseUrl}/adapters/mcp-sample-server/mcp`.

## Proving session affinity

With `-ReplicaCount 2` (or higher), MCP Gateway must pin a multi-call session
to the *same* replica for `describe_environment` to keep reporting the same
`pod_name` -- that pinning is exactly what the `mcp-session-id` header (or a
`session_id` query parameter) exists to drive.

1. Call `initialize` once, capture the returned `mcp-session-id` header value.
2. Call `tools/call` for `describe_environment` several times, **passing that
   same `mcp-session-id` header each time**. Observe `pod_name` stays
   constant.
3. Now call `tools/call` for `describe_environment` again but **omit the
   header (or use a freshly generated one)** on each call. Observe `pod_name`
   varies across replicas as requests are load-balanced without affinity.

Also inspect `forwarded_user_id` / `forwarded_roles` in the response across
calls made through the fully governed path (APIM subscription key + Entra
bearer token) versus calling the adapter's `/mcp` endpoint directly -- this is
how you demonstrate Identity Mode B end-to-end identity propagation, not just
transport-level pass-through.

## Validate through the governance plane

Once published through the Citadel Publish Contract, use
`scripts/validate_mcp_endpoint.py` against the APIM-fronted URL to check the
full path (DNS/TLS/APIM subscription key/Entra token/MCP handshake) in one
command -- see that script's `--help` for details.

---

*Last updated: 2026-08-14*
