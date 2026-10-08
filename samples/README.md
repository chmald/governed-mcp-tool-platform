# Samples — Expansion Scenario Catalog

This pattern ships with exactly one working reference server and four **design-and-integration guides** for turning this landing zone into a real, differentiated platform. They are different things, used differently.

---

## The reference server vs. these scenarios

| | [`mcp-server/`](./mcp-server/README.md) | `scenarios/` (this catalog) |
|---|---|---|
| What it is | A minimal, dependency-free, **running** Streamable HTTP MCP server | Four **guides** — architecture deltas, registration payloads, gotchas |
| Purpose | Prove the platform works: session affinity and identity propagation are visible in its `describe_environment` tool | Show how to point the *same* platform at a real, differentiated workload |
| Tools | `echo`, `describe_environment` — intentionally trivial | N/A — each scenario describes a category of real tool, not a specific one |
| Code you maintain | Yes, it's yours to fork | Varies by scenario — from *none* (scenario 2) to *all of it* (scenario 3) |
| Ready to deploy as-is | Yes | No — each is a starting point you adapt to a real tool/workload |

The reference server exists to prove the seam between the two planes works. It is deliberately too trivial to be an example of *what to build* — that's what this catalog is for. The originating requirement for this pattern was that it "can be used to expand on several examples" and be "a good starting point for several projects." This catalog is that expansion surface, kept generic and domain-agnostic so it applies to developer-platform tools, line-of-business data tools, operational tooling, or partner-facing catalogs alike — the same way the rest of this pattern is generic on purpose (see [`../README.md`](../README.md)).

Every scenario below assumes the baseline platform (both planes, per [`../01-architecture.md`](../docs/01-architecture.md)) is **already deployed** — these are extensions on top of a working landing zone, not alternate ways to stand one up. Start at [`../02-prerequisites.md`](../docs/02-prerequisites.md) / [`../03-deployment.md`](../docs/03-deployment.md) if that isn't true yet.

---

## Decision table — what to start from

| What the customer wants | Start from | What it exercises | Rough effort |
|---|---|---|---|
| "We found (or already use) a community/third-party MCP server that only speaks **stdio**" — true of most community MCP servers today | [`01-stdio-bridge`](./scenarios/01-stdio-bridge.md) | `mcp-proxy` adapter bridging, the upstream trust boundary, tool-surface allow-listing as the primary compensating control | Low–Medium — hours once the image is vetted and built; most of the effort is the trust review, not the wiring |
| "Our 'tools' are already REST APIs onboarded in API Management" | [`02-rest-api-as-mcp`](./scenarios/02-rest-api-as-mcp.md) | `assetType: mcp-from-api`, zero additional compute, the internal-hop 401 trap | Low — hours; no runtime plane, no new compute, just a Publish Contract asset |
| "A tool must reach Azure resources on its own credentials and enforce per-user authorization itself" | [`03-workload-identity-data-tool`](./scenarios/03-workload-identity-data-tool.md) | Workload identity federation, the admin-gated privilege-escalation review, least-privilege RBAC, Mode B forwarded identity | Medium–High — needs a security/RBAC review cycle in addition to the wiring |
| "We have, or will accumulate, many small tools and want one MCP surface instead of many adapter endpoints" | [`04-multi-tool-catalog`](./scenarios/04-multi-tool-catalog.md) | Tool Gateway Router aggregation, `POST /tools`, the `GatewaySettings__Secret` trust boundary | Medium — modest per additional tool, but front-loaded by the shared-secret and allow-list review |

None of these are mutually exclusive with the reference server — the reference server can keep running as a smoke-test adapter alongside any of them.

---

## Capability matrix

| Scenario | Session affinity needed? | Workload identity needed? | Tool allow-listing relevant? | Identity mode | Where the tool logic lives |
|---|---|---|---|---|---|
| *Reference server (baseline, for comparison)* | Yes | No | Yes — same compensating control as any published asset | B (default) | `samples/mcp-server/` — code you own and can audit fully |
| **01 — stdio bridge** | Yes — same StatefulSet/session model as any adapter | Not by default; only if the bridged tool itself needs Azure (compose with scenario 03) | **Yes — the primary compensating control**, since there is no image allow-listing upstream | B (default), but the bridged code likely can't *use* the forwarded headers — see the scenario for why | Inside the bridged third-party stdio process — code you did **not** write and must treat as partially trusted |
| **02 — REST-as-MCP** | No — no runtime plane, no pods at all | No | Not via the `mcp-gateway` mechanism — governed instead by which `operationNames` you expose | N/A — no MCP runtime; identity is whatever the source API already enforces | Entirely inside the already-existing REST API you onboarded |
| **03 — workload identity data tool** | Yes | **Yes — the point of the scenario** | Yes, as defense-in-depth alongside the tool's own per-user filtering | **B**, effectively required — the tool needs the forwarded identity to filter; Mode A defeats the premise | Inside your own adapter code, which calls Azure via workload identity and enforces per-user filtering from `X-Mcp-UserId` / `X-Mcp-Roles` |
| **04 — multi-tool catalog** | Yes — the router itself is a StatefulSet with its own session affinity | Optional, per registered tool — composability of `useWorkloadIdentity` with `POST /tools` is unverified; confirm before assuming | Yes — still worth keeping at the edge since the router aggregates many tool names behind one asset | B (default); depends entirely on `GatewaySettings__Secret` being a real secret | Wherever the registered tool's `port` endpoint is implemented — exact topology not fully verified; confirm before designing |

---

## Scenario docs

| Scenario | Read this when |
|---|---|
| [01 — Bridging a stdio MCP server](./scenarios/01-stdio-bridge.md) | You're integrating an existing community or third-party MCP server that only runs over stdio |
| [02 — Exposing a REST API as MCP](./scenarios/02-rest-api-as-mcp.md) | Your tools already exist as REST operations in API Management |
| [03 — A workload-identity data tool](./scenarios/03-workload-identity-data-tool.md) | A tool needs its own Azure credentials and must enforce per-user authorization |
| [04 — A multi-tool catalog via the Tool Gateway Router](./scenarios/04-multi-tool-catalog.md) | You want one dynamic MCP surface aggregating many small tools |

---

## How to combine scenarios

These are not four isolated forks of the platform — most real engagements end up composing two or more:

- **01 + 04** — Bridge several third-party stdio tools as individual adapters (scenario 01), and consider whether the ones worth a single client-visible surface belong behind the router (scenario 04) instead of as separate published assets. Note that bridged adapters register via `POST /adapters` (their own StatefulSet), not `POST /tools` — "combining" here means publishing both the router's aggregated asset and any individually significant bridged adapter as **separate assets under the same Access Contract product**, not literally merging them into one endpoint.
- **03 + 04** — A workload-identity-enabled data tool could in principle be one of the tools registered behind the router, *if* the router's `POST /tools` registration path actually supports `useWorkloadIdentity` — the verified field list in this pattern's fact base lists `useWorkloadIdentity` only among the `POST /adapters` fields, not confirmed for `POST /tools`. Verify this against the live OpenAPI spec before designing around it; do not assume it composes.
- **02 alongside anything** — `mcp-from-api` doesn't touch the runtime plane at all, so it composes trivially. Publish a `mcp-from-api` asset *and* a `mcp-existing` asset (scenario 01 or 03) under the **same** Access Contract product's `services` list, and one consumer key reaches both a REST-derived tool surface and a hosted MCP runtime surface. That's a real, useful combination and it's purely an Access Contract decision — no architecture change.
- **General rule** — Combining scenarios is a portfolio decision made at two layers: the **Access Contract** layer (which published assets share one product/key/quota envelope) and, within the runtime plane, the **registration** layer (what you register as an adapter vs. what you register as a router-aggregated tool). It's rarely an either/or across the whole platform — pick per asset, not per platform.

---

## See also

- [`../README.md`](../README.md) — pattern overview and locked decisions
- [`../01-architecture.md`](../docs/01-architecture.md) — the two-plane model, identity modes, the streaming constraint
- [`../docs/06-mcp-publishing-and-access-contracts.md`](../docs/06-mcp-publishing-and-access-contracts.md) — the Publish/Access Contract integration seam these scenarios build on
- [`../docs/07-identity-and-token-flow.md`](../docs/07-identity-and-token-flow.md) — the three identity modes referenced throughout the capability matrix
- [`mcp-server/README.md`](./mcp-server/README.md) — the reference server these scenarios extend beyond

---

*Last updated: 2026-08-14*
