[README](../README.md) › [docs index](./00-reproduce-this-demo.md) › 01 Architecture

# 01 — Architecture

<p align="center">
  <img src="./assets/icons/api-management.svg" width="40" alt="API Management"/>&nbsp;&nbsp;
  <img src="./assets/icons/application-gateway.svg" width="40" alt="Application Gateway"/>&nbsp;&nbsp;
  <img src="./assets/icons/aks.svg" width="40" alt="AKS"/>&nbsp;&nbsp;
  <img src="./assets/icons/entra-id.svg" width="40" alt="Microsoft Entra ID"/>&nbsp;&nbsp;
  <img src="./assets/icons/log-analytics.svg" width="40" alt="Log Analytics"/>&nbsp;&nbsp;
  <img src="./assets/icons/event-hubs.svg" width="40" alt="Event Hubs"/>
</p>

<p align="center">
  <img src="./assets/badges/version.svg" alt="pattern version"/>
  <img src="./assets/badges/static-only.svg" alt="static-only"/>
</p>

Reference architecture for the **Secure MCP Gateway Landing Zone**. Read this first, then move to [02-prerequisites.md](./02-prerequisites.md).

## At a glance

| | Item | Value |
|---|---|---|
| <img src="./assets/icons/api-management.svg" width="24" alt=""/> | Plane 1: governance edge | API Management (native MCP API type) enforces who may call, within what quota, and meters usage |
| <img src="./assets/icons/aks.svg" width="24" alt=""/> | Plane 2: MCP runtime | `mcp-gateway` on AKS decides what runs and how it scales; owns session affinity |
| <img src="./assets/icons/gear.svg" width="24" alt=""/> | Integration seam | The `mcp-existing` Publish Contract asset type, pointed at the runtime's TLS-terminated origin |
| <img src="./assets/icons/entra-id.svg" width="24" alt=""/> | Default identity mode | Mode B, caller-token passthrough: the runtime sees the real end user |
| <img src="./assets/icons/alerts.svg" width="24" alt=""/> | Hard constraint | Policies stay inbound-only: reading an MCP response body breaks streaming |

---

## Goals

| | Goal |
|---|---|
| <img src="./assets/icons/policy.svg" width="20" alt=""/> | Give MCP tool traffic the **same governance envelope** an organization already applies to LLM traffic: authentication, per-consumer quota, metering, chargeback, private networking, and centralized observability |
| <img src="./assets/icons/toolbox.svg" width="20" alt=""/> | Let platform teams **onboard arbitrary MCP servers dynamically** — self-service, no platform redeploy — while keeping who-may-call-what under central control |
| <img src="./assets/icons/aks.svg" width="20" alt=""/> | Preserve **stateful MCP sessions** across horizontally scaled server replicas |
| <img src="./assets/icons/users.svg" width="20" alt=""/> | Preserve **end-user identity** end to end, so the tool runtime can make its own authorization decisions and audit real principals |
| <img src="./assets/icons/dev-console.svg" width="20" alt=""/> | Make the platform's **boundaries explicit** — particularly the controls that MCP's streaming nature makes impossible — rather than leaving them to be discovered in a pilot |

## Non-goals

| | Non-goal |
|---|---|
| <img src="./assets/icons/api-management.svg" width="20" alt=""/> | **Not** a replacement for API Management's native *expose REST API as MCP server* capability. If your tools are already REST APIs in API Management, use that directly — this landing zone is for hosting arbitrary MCP server workloads |
| <img src="./assets/icons/foundry-agent-service.svg" width="20" alt=""/> | **Not** a general agent runtime. Agent orchestration, planning, and memory live above this layer |
| <img src="./assets/icons/entra-roles.svg" width="20" alt=""/> | **Not** a per-tool authorization engine inside the MCP runtime. Fine-grained control is enforced at the edge in v1 (see [Compensating controls](#compensating-controls)) |
| <img src="./assets/icons/content-safety.svg" width="20" alt=""/> | **Not** a response-inspection platform for MCP. Streaming makes that architecturally impossible here — see [The streaming constraint](#the-streaming-constraint-read-this-before-designing-policy) |
| <img src="./assets/icons/folder.svg" width="20" alt=""/> | **Not** tied to a tool domain, industry, or use case |

---

## The two-plane model

The architecture separates **who may call** from **what runs and how it scales**. That separation is the whole idea.

[![Secure MCP Gateway two-plane architecture: consumers call API Management (governance edge), which forwards over TLS to Application Gateway and the MCP Gateway on AKS, with Entra ID, observability, and FinOps as shared services](./assets/secure-mcp-gateway-architecture.png)](./assets/secure-mcp-gateway-architecture.png)

<sub>Editable source: [`assets/secure-mcp-gateway-architecture.drawio`](./assets/secure-mcp-gateway-architecture.drawio) - regenerate with `python scripts/export_diagrams.py docs/assets`.</sub>

### Why not one plane

| Attempted shortcut | Why it fails |
|---|---|
| API Management alone, MCP servers on Container Apps | No session affinity across replicas, no dynamic registration, no lifecycle management. Works for two or three static servers; collapses at fleet scale |
| MCP Gateway alone, exposed directly | No rate limiting, no per-consumer quota, no chargeback, no consumer onboarding model, no unified catalog. The runtime does not attempt these and upstream lists them as unimplemented |
| API Management native *expose REST API as MCP* only | Correct answer when tools genuinely are REST APIs. Cannot host an arbitrary MCP server container or bridge a stdio server |

Each plane covers exactly the other's gap. The pattern exists because the seam between them is already a supported extensibility point.

---

## Logical layers

| | Layer | Component | Role |
|---|---|---|---|
| <img src="./assets/icons/dev-console.svg" width="20" alt=""/> | Consumer | MCP-capable client, application, or agent | Issues MCP JSON-RPC calls over Streamable HTTP |
| <img src="./assets/icons/api-management.svg" width="20" alt=""/> | Governance | Azure API Management (native MCP API, `transportType: streamable`) | Authenticates, attributes, rate-limits, meters, logs; single front door shared with LLM traffic |
| <img src="./assets/icons/subscription.svg" width="20" alt=""/> | Governance | Access Contract product + subscription | Onboards a consuming business unit; carries the quota envelope and the revocation point |
| <img src="./assets/icons/policy.svg" width="20" alt=""/> | Governance | Policy fragments (`security-handler`, `mcp-usage`) | Subscription-key and optional JWT validation; inbound request metering into the FinOps pipeline |
| <img src="./assets/icons/api-management.svg" width="20" alt=""/> | Governance | Publish Contract backend + circuit breaker | Dedicated API Management backend per published asset with failure isolation |
| <img src="./assets/icons/api-center.svg" width="20" alt=""/> | Discovery | Azure API Center | Catalog of published MCP assets with ownership, lifecycle stage, and classification metadata |
| <img src="./assets/icons/application-gateway.svg" width="20" alt=""/> | Edge | Application Gateway | TLS termination in front of the MCP runtime |
| <img src="./assets/icons/aks.svg" width="20" alt=""/> | Runtime | MCP Gateway service | Control-plane REST API (register/update/delete MCP servers) plus data-plane reverse proxy |
| <img src="./assets/icons/cosmos-db.svg" width="20" alt=""/> | Runtime | Session routing layer + session store | Pins an MCP session to a specific server pod for the session's lifetime |
| <img src="./assets/icons/toolbox.svg" width="20" alt=""/> | Runtime | Tool Gateway Router | Aggregates individually registered tools into one dynamic MCP surface and forwards calls by tool name |
| <img src="./assets/icons/aks.svg" width="20" alt=""/> | Runtime | MCP server adapters | The actual MCP servers, as StatefulSets with stable pod identity |
| <img src="./assets/icons/entra-id.svg" width="20" alt=""/> | Identity | Microsoft Entra ID | One app registration; issues tokens both planes validate; carries `mcp.admin` / `mcp.engineer` app roles |
| <img src="./assets/icons/log-analytics.svg" width="20" alt=""/> | Observability | Log Analytics + Application Insights | Unified telemetry across both planes |
| <img src="./assets/icons/event-hubs.svg" width="20" alt=""/> | FinOps | Event Hub → Logic App → Cosmos DB | Usage events for chargeback and showback by product, consumer, and asset |

---

## The integration seam

This is the load-bearing part of the pattern. The accelerator ships a **Publish Contract** subsystem whose entire purpose is publishing non-LLM assets — tools and agents — through the same gateway. One of its asset types exists precisely for "I already have an MCP server running somewhere; govern it."

| Asset type | What it does | Resulting endpoint |
|---|---|---|
| `mcp-from-api` | Wraps an already-onboarded REST API's operations as MCP tools; reuses that API's backend | `{gateway}/mcp/<path>/mcp` |
| **`mcp-existing`** | **Publishes a remote or self-hosted MCP server through the gateway with a dedicated backend** | **`{gateway}/mcp/<path>`** |
| `a2a` | Publishes an agent's A2A endpoint plus its agent card | `{gateway}/agent/<path>` |

This pattern uses **`mcp-existing`**, pointing at the MCP runtime's TLS-terminated public origin.

What that single declaration produces:

| Step | Produces | Detail |
|---|---|---|
| 1 | An API Management **backend** resource | For the MCP runtime origin, with a **circuit breaker** (default: 3 failures within `PT5M` trips for `PT1M`, honoring `Retry-After`) |
| 2 | An API Management **API of native type `mcp`** | `transportType: streamable` — API Management understands MCP semantics rather than blind-proxying HTTP |
| 3 | The **baseline MCP policy** | Runs the shared `security-handler` fragment (subscription key, optionally per-product JWT) and the `mcp-usage` metering fragment |
| 4 | Optional **API Center registration** | Ownership, lifecycle, and classification metadata |

> [!TIP]
> Mind the two path shapes above. `mcp-existing` does **not** get a `/mcp` suffix appended, because the upstream server already serves MCP at its own path; `mcp-from-api` does, because API Management is synthesizing the MCP surface itself. Getting this backwards is the single most common 404 in this architecture.

Access is then granted **separately** by an **Access Contract**, which creates the product and subscription a consumer actually uses. Publishing and granting are deliberately two steps: publishing an asset does not entitle anyone to call it.

Deep dive: [06-mcp-publishing-and-access-contracts.md](./06-mcp-publishing-and-access-contracts.md)

---

## Request path, end to end

[![Request path sequence: MCP client acquires a token from Entra ID, calls API Management with a subscription key and bearer token, API Management forwards over TLS through Application Gateway to the MCP Gateway, which re-validates the token against Entra ID and routes to a pinned MCP server pod](./assets/01-architecture-request-path.png)](./assets/01-architecture-request-path.png)

<sub>Editable source: [`assets/01-architecture-request-path.drawio`](./assets/01-architecture-request-path.drawio) - regenerate with `python scripts/export_diagrams.py docs/assets`.</sub>

> [!NOTE]
> The critical property: **two independent validations of the same token**. API Management governs *entitlement* — is this consumer allowed, and within quota. The MCP runtime governs *identity* — who is this principal, and what may they reach. Neither trusts the other's decision, and neither has to.

---

## Identity: the central design trade-off

An MCP call through this architecture must satisfy two different authorization systems. How you bridge them is the most consequential decision in the pattern.

| Mode | Backend `authType` | Who the runtime sees | Runtime RBAC | Use when |
|---|---|---|---|---|
| **A — Managed identity** | `managed-identity` with audience `api://<CLIENT_ID>` | API Management's service principal | Collapses to "API Management is one trusted caller" | Consumers are machine workloads with no user context, and all authorization belongs at the edge |
| **B — Caller-token passthrough** ⭐ **default** | `none` | The **real end user or workload** | Fully preserved | You need end-user attribution, per-principal audit, or the runtime's own RBAC |
| **C — Split plane** | `managed-identity` for the governed path | Both, depending on route | Preserved on the direct path | Machine traffic is governed at the edge while interactive developers connect directly using standard OAuth discovery |

**Why Mode B is the default and why it works:** the baseline MCP policy applied by the Publish Contract does *not* rewrite the `Authorization` header — it only runs the security and metering fragments. With backend `authType: none`, API Management injects no credential of its own, so the caller's original bearer token arrives at the MCP runtime intact. The runtime validates it independently against Entra and derives genuine end-user identity.

The requirement this creates: the caller's token audience must be the MCP runtime's application ID URI. Consumers acquire a token for `api://<CLIENT_ID>/.default` and *additionally* present the API Management subscription key. Two credentials, two purposes:

| | Credential | Answers |
|---|---|---|
| <img src="./assets/icons/keys.svg" width="20" alt=""/> | **Subscription key** | *Which consuming business unit is this, and what is their quota?* |
| <img src="./assets/icons/entra-id.svg" width="20" alt=""/> | **Bearer token** | *Which principal is this, and what may they reach?* |

Mode A is a genuine confused-deputy trade-off, not a bug. It is correct when consumers have no user identity to preserve. It is wrong when you need to answer "who called this tool" in an audit.

Deep dive: [07-identity-and-token-flow.md](./07-identity-and-token-flow.md)

---

## Session affinity: what actually makes this hard

MCP sessions are stateful. A naive load balancer sends the second call in a session to a different replica, which has never seen the handshake, and the session breaks.

The MCP runtime solves this in its application layer rather than relying on ingress stickiness:

| Step | What the runtime does |
|---|---|
| 1 | MCP servers deploy as **StatefulSets** behind **headless Services**, giving every pod a stable DNS identity |
| 2 | The runtime watches the Kubernetes API for the live set of **ready** pods per server |
| 3 | A **new** session is assigned a random healthy pod |
| 4 | The session identifier returned by the server is stored against a **scoped key** — authenticated principal, server name, session id |
| 5 | **Subsequent** calls carrying that session identifier are routed to the same pod |
| 6 | Session identifiers are validated against a strict character allow-list before use |

Two consequences worth internalizing:

- **The scoped key includes the authenticated principal.** A session identifier cannot be replayed by a different caller, even one who legitimately has access to the same server. This is why Mode B identity matters beyond audit — it makes session scoping meaningful per user rather than per gateway.
- **StatefulSet plus headless Service is not incidental.** Deploying an MCP server as a plain Deployment with a ClusterIP Service breaks affinity, because there is no stable per-pod DNS name to pin to.

**Protocol-era caveat.** This design assumes the session-oriented MCP protocol, where a handshake establishes a session identifier. A newer specification revision removes protocol-level sessions in favor of stateless per-request metadata. Upstream tracking for dual-era support is open at the time of writing. Pin your client and server SDK versions deliberately and validate against the exact revision your client speaks — see [05-troubleshooting.md](./05-troubleshooting.md).

---

## The streaming constraint (read this before designing policy)

> [!WARNING]
> MCP responses stream over Server-Sent Events. **Reading the response body in an API Management policy forces buffering, which breaks streaming.** Policies must stay **inbound-only**: never read `context.Response.Body`. The baseline MCP policy says so explicitly and deliberately performs no outbound inspection.

What this means concretely:

| Control | Available for MCP? | Where it must live instead |
|---|---|---|
| Authentication (subscription key, JWT) | ✅ Inbound | API Management |
| Rate limiting and quota | ✅ Inbound | API Management (Access Contract product) |
| Usage metering and chargeback | ✅ Inbound (request/tool-call count) | API Management → Event Hub → Cosmos DB |
| Request-side PII detection | ⚠️ Possible inbound, per-asset opt-in | Custom policy overlay on that asset |
| Request-side content safety | ⚠️ Possible inbound, per-asset opt-in | Custom policy overlay on that asset |
| **Response-side PII de-anonymization** | ❌ **Not available** | The MCP server itself |
| **Response content safety** | ⚠️ **Not via custom policy — but see `llm-content-safety` below** | Purpose-built policy, or the MCP server |
| **Token-based metering** | ❌ Not applicable | Tool calls have no token economy — the unit of account is the call, not the token |

**Do not confuse "policy cannot read the body" with "you cannot observe it."** They are different things, and conflating them understates what this architecture can actually see. Policies must stay inbound-only — but API Management understands MCP *semantically*, so it emits per-tool, per-session telemetry natively without any policy touching a response body. See [Native observability](#native-observability--richer-than-the-policy-constraint-suggests) below.

### The one documented exception — `llm-content-safety`

The blanket rule "nothing can inspect an MCP response" is **almost** true, and the exception matters.

Per Microsoft Learn (*llm-content-safety policy*), that policy *"can also enforce content safety checks on requests or responses for **MCP tools** or A2A Agent APIs managed in API Management."* It avoids the buffering trap with a **sliding window** — `window-size` and `window-overlap-size` are documented as **configurable only for responses** — evaluating the stream in chunks rather than materializing the whole body.

So the precise statement is:

| Mechanism | Response-side on MCP |
|---|---|
| Custom policy reading `context.Response.Body` | ❌ Breaks streaming — never do this |
| Citadel PII de-anonymization fragment | ❌ Requires body access; upstream states it is unsupported for MCP |
| **`llm-content-safety` with sliding window** | ⚠️ **Documented as applicable to MCP tools** — requires an Azure AI Content Safety resource and a managed-identity backend |

**This pattern does not adopt `llm-content-safety` in v1** — it adds a dependency, a per-call latency cost, and a failure mode, and the sliding-window behaviour against a specific MCP server's streamed output should be validated before anyone relies on it. It is recorded here as a **supported extension point**, not a gap. If a customer needs response-side content safety on tool output, this is the sanctioned route; verify it end to end against your own server before promising it.

Design implication: **the MCP server is a trust participant, not an untrusted upstream.** Anything that must be *enforced* on the way out has to be enforced by the server, or by that one purpose-built policy. This is why control-plane write access — the ability to register an arbitrary image as an MCP server — is a privileged operation in this architecture.

### The constraint has a scope trap

Per Microsoft Learn (*Expose and govern existing MCP server*), API Management evaluates policies at **global (All APIs) scope before MCP server scope**. Two consequences that bite in practice:

| # | Trap | Effect |
|---|---|---|
| 1 | A **global** policy that reads the response body | Breaks MCP even when every MCP-scope policy is correctly inbound-only |
| 2 | A **global diagnostic setting** that logs response payload bytes | Does the same. Microsoft's guidance is explicit: with instance-wide Application Insights / Azure Monitor logging enabled, set **"Number of payload bytes to log" for Frontend Response to `0`**, and log payloads selectively at API or MCP-server scope instead |

The failure presents as "the client hangs," not as an error — and the cause lives far away from anything MCP-related. See [05-troubleshooting.md](./05-troubleshooting.md).

---

## Native observability — richer than the policy constraint suggests

Because API Management exposes MCP as a **native API type** rather than proxying opaque HTTP, it parses the JSON-RPC envelope itself and emits structured telemetry with **zero configuration** — no policy, no body access, no streaming risk.

Per Microsoft Learn (*Monitor MCP server traffic*, last updated 2026-06-30), MCP telemetry follows the **OpenTelemetry semantic conventions for generative AI** (`gen_ai.*`) — the same convention family used for model telemetry. That shared vocabulary is what makes tool traffic and model traffic comparable on one pane of glass.

Emitted on **every** MCP request:

| Dimension | Gives you |
|---|---|
| `gen_ai.operation.name` | `tools/list` vs `tools/call` — discovery vs invocation |
| `gen_ai.conversation.id` | The **MCP session ID** — ties directly to this pattern's session-affinity model |
| `user_agent.name` / `.version` | Which MCP client (e.g. an IDE, a desktop agent) is actually calling |
| `auth.type` | Which inbound authentication method was used |
| `service.name` / `.version` | Which published MCP asset |
| `api.type` (`Mcp`) | Discriminator for querying MCP traffic specifically |
| `error.type` / `error.message` | Failure classification |
| `duration` (standard field) | Per-tool latency, chartable without configuration |

`tools/list` adds `ToolCount`. `tools/call` adds **`gen_ai.tool.name`** and `gen_ai.tool.type` — so **per-tool visibility is native**, even though per-tool *policy* is not.

Tool arguments and results (`gen_ai.tool.call.arguments` / `.result`) are available only when **per-MCP-server payload logging** is deliberately enabled. Microsoft cautions that those fields can contain prompts, customer data, or secrets — treat enablement as a data-governance decision, not a debugging convenience, and note the tension with the global-scope setting above: **global stays `0`; per-server is the selective opt-in.**

Where the built-in schema falls short — agent ID, a JWT claim, product attribution — the **`trace` policy** at MCP server scope adds custom dimensions inbound.

For the full parity analysis against model traffic, the KQL cookbook, and what genuinely cannot reach parity, see [08-observability-and-governance-parity.md](./08-observability-and-governance-parity.md).

---

## Trust boundaries

[![Trust boundaries: untrusted public clients, then Boundary 1 governance edge (API Management), Boundary 2 runtime edge (Application Gateway and MCP Gateway), Boundary 3 workload isolation (MCP server pods), then Azure resources; two weaknesses are flagged in red: the HTTP-only edge listener and direct origin access](./assets/01-architecture-trust-boundaries.png)](./assets/01-architecture-trust-boundaries.png)

<sub>Editable source: [`assets/01-architecture-trust-boundaries.drawio`](./assets/01-architecture-trust-boundaries.drawio) - regenerate with `python scripts/export_diagrams.py docs/assets`.</sub>

| Boundary | Enforced by | Fails closed? |
|---|---|---|
| Public → governance | Subscription key required; optional per-product JWT; quota and rate limit | Yes — no key, no call |
| Governance → runtime | TLS; private networking; API Management is the only sanctioned ingress path | Depends on network configuration — see below |
| Runtime → workload | Entra token validation; resource RBAC; Kubernetes NetworkPolicy default-deny; only gateway pods may reach server pods | Yes — privileged capabilities default to admin-only |
| Workload → Azure resources | Workload identity federation, gated to an admin-only role by default | Yes |

> [!WARNING]
> **Two boundary weaknesses to close during deployment, not after:**
>
> 1. **The runtime's edge listener is HTTP-only out of the box.** Until you add a TLS listener, boundary 2 is cleartext. Phase 4 of [03-deployment.md](./03-deployment.md) covers this.
> 2. **Nothing inherently prevents a client from reaching the runtime origin directly**, bypassing API Management and therefore bypassing quota and metering. Close this with private networking or an origin restriction so the runtime accepts traffic only from the gateway. Treat "the governed path is the *only* path" as a deployment requirement, not an assumption.

---

## Compensating controls

The MCP runtime does not currently provide rate limiting, per-tool authorization, or image allow-listing. The architecture supplies each at a different layer rather than pretending the gap does not exist.

| Runtime gap | Compensating control | Layer |
|---|---|---|
| No rate limiting | `rate-limit-by-key` and `quota-by-key` in the Access Contract product policy | API Management |
| No per-consumer quota | One product and subscription per consuming business unit | API Management |
| No chargeback | `mcp-usage` inbound metric → Event Hub → Logic App → Cosmos DB → reporting, plus native per-tool telemetry | FinOps pipeline + Azure Monitor |
| No per-tool authorization | **Static**: the MCP server's Tools blade selects which operations are exposed at all. **Runtime/per-consumer**: tool-surface allow-listing in the hardened policy overlay | API Management (see `infra/policies/`) |
| No image allow-listing | Registry-level policy plus treating control-plane write access as privileged | Registry + Entra roles |
| No catalog approval workflow | API Center registration as the publishing gate; publishing and granting are separate steps | API Center + Access Contract |
| Coarse in-runtime RBAC | Mode B identity passthrough preserves the principal so runtime RBAC still discriminates | Identity design |
| No per-tool observability in the runtime | Native `gen_ai.tool.name` telemetry — no policy or body access required | Azure Monitor / Application Insights |

> [!NOTE]
> **Note the asymmetry between policy and telemetry.** API Management currently applies policies to *all* operations exposed as tools in an MCP server — there is no native per-tool policy scope (per *About MCP servers in Azure API Management*). But it *does* emit per-tool telemetry natively. So you can **see** per-tool behaviour out of the box, while **enforcing** per-tool rules requires the overlay. Observability arrives before enforcement does.

---

## Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Custom reverse proxy between API Management and the runtime | The `mcp-existing` Publish Contract already does this natively, with a circuit breaker and metering. Custom glue would be unmaintained code duplicating a supported feature |
| Managed identity (Mode A) as the default | Destroys end-user attribution for every consumer, including the ones that have it. Better as an explicit opt-in for machine workloads |
| Terminating MCP sessions at API Management | API Management is not the session authority here; the runtime owns pod pinning. Splitting session state across both planes would create a consistency problem with no owner |
| Response-body scanning for MCP | Architecturally impossible without breaking streaming. Documented as a boundary instead of attempted |
| Running MCP servers as plain Deployments | Breaks session affinity — no stable per-pod identity to pin to |
| Skipping the Access Contract and granting direct API access | Loses the quota envelope, the chargeback attribution, and the single revocation point |
| Pinning upstreams to a branch instead of a SHA | One upstream has no releases or tags at all, and the other's branch is actively moving. Branch pinning is not reproducible |

---

## Scaling and resilience

| | Mechanism | Effect |
|---|---|---|
| <img src="./assets/icons/aks.svg" width="20" alt=""/> | **MCP server replicas** | Scale horizontally; session affinity keeps existing sessions intact. New sessions distribute across healthy pods |
| <img src="./assets/icons/api-management.svg" width="20" alt=""/> | **Circuit breaker** at the API Management backend | Isolates runtime failures from the shared AI gateway — important because the same gateway carries LLM traffic |
| <img src="./assets/icons/monitor.svg" width="20" alt=""/> | **Pod readiness** | Drives routing; the runtime continuously watches the ready-pod set rather than relying on stale endpoint lists |
| <img src="./assets/icons/api-management.svg" width="20" alt=""/> | **API Management scale units** | Govern edge throughput; the SKU choice is the dominant cost and capacity lever |
| <img src="./assets/icons/cosmos-db.svg" width="20" alt=""/> | **Session state** | Persists in a distributed store, so gateway restarts do not orphan every in-flight session — but verify the store's containers are provisioned before relying on preview session features <img src="./assets/badges/preview.svg" alt="preview"/> |

---

## What to change per engagement

Everything below is configuration, not architecture:

| | Change | What it is |
|---|---|---|
| <img src="./assets/icons/aks.svg" width="20" alt=""/> | The **MCP servers** | Registered as adapters (this is the actual use case) |
| <img src="./assets/icons/subscription.svg" width="20" alt=""/> | The **Access Contract products** | One per consuming business unit, with their quota envelopes |
| <img src="./assets/icons/resource-group.svg" width="20" alt=""/> | The **region** and **API Management SKU** | Driven by sizing and residency |
| <img src="./assets/icons/entra-id.svg" width="20" alt=""/> | The **identity mode** | Change only if Mode B's audience requirement does not fit the consumer population |
| <img src="./assets/icons/policy.svg" width="20" alt=""/> | The **hardened policy overlay** | Which tool surfaces are allow-listed per asset |
| <img src="./assets/icons/api-center.svg" width="20" alt=""/> | **API Center** registration | Whether it is enabled |

> [!IMPORTANT]
> The two-plane split, the `mcp-existing` seam, the inbound-only policy rule, and the StatefulSet session model stay constant.

---

Next: [02 Prerequisites →](./02-prerequisites.md)

*Last updated: 2026-10-02*
