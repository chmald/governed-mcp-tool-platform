# infra/ — Infrastructure assets

Declarative assets for the integration seam between the two planes. **This folder does not deploy the planes themselves** — those come from the two upstream repositories. What lives here is the glue that makes them one governed platform, expressed as configuration rather than code.

| Path | What it is | Applied during |
|---|---|---|
| `publish-contract/mcp-gateway.bicepparam` | Publishes the MCP runtime through API Management as a governed MCP asset | [Phase 6](../docs/03-deployment.md#phase-6--publish-the-mcp-asset-through-api-management) |
| `access-contract/mcp-consumer.bicepparam` | Grants a consuming business unit an entitled subscription with quota | [Phase 7](../docs/03-deployment.md#phase-7--grant-governed-access) |
| `policies/mcp-hardened-policy.xml` | Optional overlay adding tool-surface allow-listing, a per-asset rate ceiling, and custom trace enrichment for MCP telemetry | [Phase 6.4](../docs/03-deployment.md#64-optional--apply-the-hardened-policy-overlay) |
| `modules/` | Reserved for engagement-specific Bicep modules | As needed |

Conceptual detail lives in [docs/06-mcp-publishing-and-access-contracts.md](../docs/06-mcp-publishing-and-access-contracts.md).

---

## How the parameter files are used

They are **templates for the upstream accelerator's own parameter files**, not standalone deployments. The workflow is:

1. Clone the accelerator at the pinned commit
2. Copy the template over the accelerator's corresponding `main.bicepparam`
3. Replace every `<PLACEHOLDER>`
4. Deploy against the accelerator's `main.bicep`

```
ai-hub-gateway-solution-accelerator/
└── bicep/infra/
    ├── citadel-publish-contracts/
    │   ├── main.bicep          ← template (upstream)
    │   └── main.bicepparam     ← replace with publish-contract/mcp-gateway.bicepparam
    └── citadel-access-contracts/
        ├── main.bicep          ← template (upstream)
        └── main.bicepparam     ← replace with access-contract/mcp-consumer.bicepparam
```

> **Diff before every deploy.** Both contract subsystems are **versioned and preview**. Compare the accelerator's own `main.bicepparam` at the pinned commit against these templates before deploying, and again after any upstream bump. A silently changed parameter name fails at deploy; a silently changed *default* fails in production.

---

## Non-negotiables

### The backend URL must be HTTPS

The MCP runtime ships an HTTP-only edge listener. [Phase 4](../docs/03-deployment.md#phase-4--harden-the-mcp-runtime-edge) adds TLS. Publishing against `http://` puts every bearer token on the wire in cleartext between the two planes.

### A policy overlay must keep `<base />`

Supplying `policyXml` **replaces** the baseline MCP policy for that asset. `mcp-hardened-policy.xml` therefore opens `<inbound>` with `<base />` to inherit the shared `security-handler` and `mcp-usage` fragments.

> Removing that single line silently disables authentication and metering while the endpoint keeps working. It is the most dangerous one-line edit in this architecture.

### MCP policies are inbound-only

MCP responses stream over Server-Sent Events. Reading `context.Response.Body` — directly, or implicitly via outbound `validate-content` or body transforms — forces buffering and breaks streaming.

Reading `context.Request.Body` **is** safe, always with `preserveContent: true`.

This constraint applies to product policies from the Access Contract too, not just the API-scoped overlay.

---

## Before applying the hardened overlay

1. Create the `mcp-allowed-tools` named value in API Management — comma-separated tool names, or `*` to disable allow-listing explicitly
2. Replace `RATE_LIMIT_CALLS`, `RATE_LIMIT_PERIOD`, and `MAX_REQUEST_BYTES` in the policy file
3. **Validate the XML parses** (see below) — an unescaped quote fails at deploy time, not at edit time
4. Uncomment the `policyXml` line on the asset in the publish contract
5. Redeploy, then run [04-testing.md](../docs/04-testing.md) §4, §7, and §8 — §7 confirms you did not break streaming, §8 confirms the telemetry claims (native and the added trace) actually hold

### Escaping — the trap when editing policy expressions

Policy expressions live inside XML **attributes** (`condition="…"`, `value="…"`), so every double quote inside a C# expression must be written as `&quot;`, and `<` / `>` as `&lt;` / `&gt;`. A literal `"` terminates the attribute and produces a parse error.

```
WRONG:  condition="@(context.Variables["x"] == "y")"
RIGHT:  condition="@(context.Variables[&quot;x&quot;] == &quot;y&quot;)"
```

Expressions in element **content** (inside `<value>` or `<set-body>`) need no escaping — which is why the same file mixes both styles.

Always validate before deploying:

```powershell
[xml](Get-Content infra/policies/mcp-hardened-policy.xml -Raw)   # throws on malformed XML
```

```bash
python -c "import xml.dom.minidom as m; m.parse('infra/policies/mcp-hardened-policy.xml'); print('valid')"
```

What the overlay adds, and why:

| Control | Compensates for |
|---|---|
| Tool-surface allow-listing | The MCP runtime has no per-tool authorization today |
| Per-asset rate ceiling | The MCP runtime has no rate limiting today |
| Payload size guard | Rejects oversized envelopes before they reach the runtime |
| Correlation id | Traces one logical call across both planes |
| Custom trace enrichment | Adds agent, subscription, and product attribution alongside the JSON-RPC method — dimensions the built-in `gen_ai.*` telemetry schema does not carry |

Only `tools/call` is gated. `initialize`, notifications, and `tools/list` pass freely so discovery still works — allow-listing controls *invocation*, not *visibility*.

---

## Observability: native telemetry and the trace policy addition

Azure API Management emits MCP telemetry to Application Insights **with zero configuration** — OpenTelemetry `gen_ai.*` fields (operation name, tool name, session/conversation id, user agent, auth type) on every request (Microsoft Learn, "Monitor MCP server traffic", last updated 2026-06-30). `mcp-hardened-policy.xml` adds one thing on top of that: an inbound-only `trace` policy step that stamps each request with custom dimensions the built-in schema does not carry — the correlation id, a caller-supplied agent id (`x-agent-id` header, defaulted safely when absent), the resolved subscription/product name, and the JSON-RPC method — all in one correlated trace event.

Two logging surfaces are easy to conflate, and only one of them is safe to leave on:

| Scope | Setting | Default posture |
|---|---|---|
| Global (All APIs) diagnostic logging | "Number of payload bytes to log" for **Frontend Response** | Must stay `0` — a non-zero value interferes with MCP streaming for every asset (Microsoft Learn, "Expose and govern existing MCP server") |
| Per-MCP-server diagnostic logging | **Frontend and Backend payload logging**, enabled per server under its own Settings → Diagnostic Logs | Off by default; a deliberate opt-in that unlocks `gen_ai.tool.call.arguments`/`.result` — these can contain prompts, customer data, or secrets, so treat the toggle itself as sensitive |

The same evaluation order that governs policies governs diagnostic settings: **API Management evaluates global (All APIs) scope before MCP server scope.** A policy set that is clean at MCP scope is not sufficient on its own — a global-scope policy or diagnostic setting that touches the response body breaks streaming regardless of what `mcp-hardened-policy.xml` does.

See [04-testing.md](../docs/04-testing.md) §8 for the runnable verification suite, and [docs/08-observability-and-governance-parity.md](../docs/08-observability-and-governance-parity.md) for the fuller observability-and-governance-parity narrative.

---

## Secrets

Nothing in this folder contains a secret, and nothing in it should ever gain one.

- Subscription keys come from **Key Vault**, populated by the Access Contract with `useTargetAzureKeyVault: true`. With `false`, keys land in deployment outputs and CI logs.
- Backend credentials, when a mode requires them, come from Key Vault or API Management named values — never a literal in a parameter file.
- Populated identifiers live in `demo-ids.local.json` (gitignored), never in `demo-ids.template.json`.

---

*Last updated: 2026-08-14*
