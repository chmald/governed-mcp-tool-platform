# Changelog

All notable changes to the **Secure MCP Gateway Landing Zone** pattern.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/). Dates are ISO `YYYY-MM-DD`.

---

## 2026-08-14 — Alignment review against Microsoft Learn

Reviewed the API Management side of the pattern against current Microsoft Learn documentation for MCP server support. **Everything below is a correction to this pattern, not a change in upstream behaviour.**

### Corrected — the pattern understated what is possible

- **Native MCP telemetry was missing entirely.** API Management emits an Application Insights `requests` row for **every** MCP request with **zero configuration**, following the **OpenTelemetry semantic conventions for generative AI** (`gen_ai.*`) — the same convention family used for model telemetry. That includes `gen_ai.tool.name` (per-tool), `gen_ai.conversation.id` (MCP session ID), `user_agent.name`/`.version` (which client), `auth.type`, `service.name`, `api.type`, `error.type`, and standard `duration`. The pattern previously described MCP metering as request-count only, which materially undersold the observability story. Added `docs/08-observability-and-governance-parity.md`, a native-observability section in `01-architecture.md`, and a full observability test suite in `04-testing.md`.
- **"Response-side controls are impossible" was too absolute.** Custom policies still must never read `context.Response.Body`. But the purpose-built **`llm-content-safety`** policy is documented as applicable to **MCP tools**, using a sliding window (`window-size` / `window-overlap-size`, configurable only for responses) instead of buffering. Locked decision #8 now distinguishes: response **PII de-anonymization** genuinely unavailable; response **content safety** available via that one policy, recorded as a supported extension not adopted in v1.
- **Per-tool control was described as absent.** Policies do apply to all tools (no per-tool policy scope), but the MCP server's **Tools blade** provides static exposure control. Documented as complementary to the hardened overlay: the Tools blade trims what exists, the overlay differentiates who may call what.
- **MCP resources are supported**; only **prompts** are unsupported for external servers. The README previously implied the whole surface was tool-only.

### Added — requirements and failure modes that were missing

- **External MCP servers must conform to MCP `2025-06-18` or later.** Documented as a compatibility gate that cuts **both ways** for this pattern's runtime: too old fails API Management's floor, while clients on the newer stateless revision outrun the runtime. Verify via the negotiated `protocolVersion`.
- **Global-scope diagnostic logging breaks MCP.** With instance-wide Application Insights logging, "Number of payload bytes to log" for **Frontend Response must be `0`**. Compounding this: API Management evaluates **global-scope policies before MCP server scope**, so a clean MCP-scope policy set is not sufficient on its own. This presents as "the client hangs," with a cause that lives nowhere near MCP.
- **Per-MCP-server payload logging** is the correct, deliberate opt-in for `gen_ai.tool.call.arguments` / `.result` — with the caution that those fields can carry prompts, customer data, or secrets. Global stays `0`; per-server is selective.
- **`Authorization` header forwarding is not guaranteed.** Microsoft documents that request headers forward *"with certain exclusions"* and does not enumerate them. Since **Identity Mode B depends entirely on that header arriving intact**, it is now documented as verify-per-environment, with the explicit `set-header` / credential-manager remedy and a diagnostic procedure.
- **Workspaces do not support MCP servers.**
- Seven new troubleshooting symptoms (sym-20 … sym-26) and a new prerequisites section covering supported SKU tiers, the protocol gate, policy/logging evaluation order, Application Insights linkage, and header forwarding.

### Added — capability

- **`trace` policy enrichment** in `infra/policies/mcp-hardened-policy.xml`, adding correlation ID, agent ID, subscription name, product name, and JSON-RPC method as custom telemetry dimensions — closing the attribution gap the built-in `gen_ai.*` schema leaves open. Inbound-only, no new named value, no response-body access.

### Confirmed by primary source

- The **inbound-only / no-`context.Response.Body`** constraint is stated independently by Microsoft in two MCP articles — validating the pattern's central architectural decision.
- **StandardV2 supports MCP servers.** Supported tiers: `Developer`, `Basic`, `Basic v2`, `Standard`, `Standard v2`, `Premium`, `Premium v2`. **Consumption is not supported.**
- Associating an MCP server with **Products** is the documented access-management route — validating the Access Contract design.

---

## 2026-08-14 — Initial pattern authoring (v1 baseline)

### Added

- Initial locked v1 architecture combining two upstream projects into a single governed MCP platform:
  - **Governance plane** — Citadel Governance Hub (`Azure-Samples/ai-hub-gateway-solution-accelerator` @ `citadel-v1`, pinned `06f7ea6c`)
  - **MCP runtime plane** — `microsoft/mcp-gateway` on AKS (pinned `59201a91`)
- Six standard documents: `README.md`, `01-architecture.md`, `02-prerequisites.md`, `03-deployment.md`, `04-testing.md`, `05-troubleshooting.md`
- Deep-dive runbooks:
  - `docs/00-reproduce-this-demo.md` — stand-up orchestrator with time budget and per-part checkpoints
  - `docs/06-mcp-publishing-and-access-contracts.md` — the Publish Contract / Access Contract integration seam
  - `docs/07-identity-and-token-flow.md` — the three identity modes and end-to-end token flow
- Infrastructure assets: hardened MCP APIM policy overlay, publish-contract and access-contract parameter templates
- Operator scripts: `Register-McpAdapter.ps1`, `Publish-McpAsset.ps1`, `validate_mcp_endpoint.py`
- Reference MCP server sample (`samples/mcp-server/`) with `echo` and `describe_environment` tools, purpose-built to make session affinity and identity propagation visible in a demo
- Test suites for the validator and the sample server
- Expansion scenario catalog (`samples/README.md` + `samples/scenarios/`) — four concrete, domain-agnostic patterns for extending the reference server into a real use case: bridging a stdio MCP server via the `mcp-proxy` adapter, exposing a REST API as MCP (`mcp-from-api`), a workload-identity data tool, and a multi-tool catalog via the Tool Gateway Router
- Azure DevOps multi-stage pipeline with approval gates between planes
- `.gitignore` and `demo-ids.template.json` per the Demos folder convention
- Presentation-ready `.drawio` diagrams (`docs/assets/`) — two-plane reference architecture and the Mode B identity/token flow with a side-by-side comparison of all three identity modes

### Key decisions locked

- **Identity Mode B (caller-token passthrough) is the default.** Verified that Citadel's `baseline-mcp-policy.xml` does not strip the `Authorization` header, so with backend `authType: none` the caller's Entra token reaches the MCP Gateway intact and its per-user RBAC continues to work while API Management still enforces subscription key, quota, and metering.
- **`assetType: mcp-existing` is the integration seam** — no custom glue code. It produces a native API Management API of `type: 'mcp'` with `transportType: 'streamable'`.
- **All MCP policy processing is inbound-only** because MCP streams over SSE and reading the response body triggers buffering that breaks streaming.
- **Both upstreams are pinned by commit SHA** — `microsoft/mcp-gateway` publishes no releases and no tags, and `citadel-v1` is a moving branch.

### Known upstream issues carried into this pattern

- `microsoft/mcp-gateway` ships an **HTTP-only** Application Gateway listener; TLS must be added before API Management fronts it.
- `microsoft/mcp-gateway` session affinity assumes the **legacy** MCP session protocol; the `2026-07-28` stateless revision is not yet supported (upstream issue #98).
- The accelerator's guides reference `main.parameters.dev.bicepparam` and siblings that **do not exist** on `citadel-v1`; cost tuning is done with `azd env set`.
- `mcp-gateway` has **no per-tool authorization, no rate limiting, and no image allow-listing** — this pattern compensates for all three at the API Management edge.

### Fixed during authoring (found by verification, not by review)

These were caught by empirical testing and an adversarial audit pass before first release. Recorded because each represents a trap a future contributor could reintroduce.

- **Invalid policy XML.** Unescaped double quotes inside C# expressions in XML *attributes* broke the attribute delimiters, which would have failed at deploy time. Rewritten with `&quot;` / `&lt;` / `&gt;` escaping; the escaping rule and a validation command are now documented in `infra/README.md` and at the top of the policy file itself.
- **`Publish-McpAsset.ps1` hung indefinitely under `-WhatIfOnly`.** The Phase 0 auth block unconditionally invoked an interactive `az login` when no active session was detected. Now fails fast with an actionable message instead.
- **`Register-McpAdapter.ps1` hung indefinitely against an unresponsive gateway.** No `-TimeoutSec` on any `Invoke-RestMethod`, and the idempotency check ran before the `ShouldProcess` gate. Now bounded by an explicit request timeout.
- **`Register-McpAdapter.ps1` leaked the full bearer token under `-Debug`.** PowerShell's own `Invoke-RestMethod` debug tracing dumps all outbound headers verbatim, including `Authorization` — triggered by the *caller's* ambient `$DebugPreference`, so it was invisible to static review of the script. All request calls now pass `-Verbose:$false -Debug:$false` as an explicit secret-leakage guard.
- **Pipeline referenced repo-local paths that do not exist.** The `DeployMcpPlane` stage pointed at an `infra/modules/mcp-gateway/main.bicep` and a `k8s/mcp-gateway/` directory that this repo deliberately does not contain — the MCP runtime plane deploys **from the pinned upstream clone**, not from a fork of its infrastructure. The stage now clones `microsoft/mcp-gateway` at the pinned SHA and uses the upstream Bicep and manifest, and gained a gate that fails the deployment when the runtime edge has no HTTPS listener.
- **Three broken in-page anchor links** across `README.md`, `03-deployment.md`, and `infra/README.md`, caused by mis-derived heading slugs.

---

*Last updated: 2026-08-14*
