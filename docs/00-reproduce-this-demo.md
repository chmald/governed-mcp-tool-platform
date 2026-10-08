[README](../README.md) › 00 Reproduce this demo

# 00 — Reproduce This Demo

<p align="center">
  <img src="./assets/icons/subscription.svg" width="40" alt="Subscription"/>&nbsp;&nbsp;
  <img src="./assets/icons/entra-id.svg" width="40" alt="Microsoft Entra ID"/>&nbsp;&nbsp;
  <img src="./assets/icons/api-management.svg" width="40" alt="API Management"/>&nbsp;&nbsp;
  <img src="./assets/icons/aks.svg" width="40" alt="AKS"/>&nbsp;&nbsp;
  <img src="./assets/icons/application-gateway.svg" width="40" alt="Application Gateway"/>&nbsp;&nbsp;
  <img src="./assets/icons/key-vault.svg" width="40" alt="Key Vault"/>
</p>

<p align="center">
  <img src="./assets/badges/version.svg" alt="pattern version"/>
  <img src="./assets/badges/static-only.svg" alt="static-only"/>
</p>

Single-page orchestrator for standing up the **Secure MCP Gateway Landing Zone** from nothing. Use this when you want checkpoints and a time budget rather than the full narrative in [03-deployment.md](./03-deployment.md).

Each part links to the authoritative detail. **This page is the map, not the territory** — do not skip the linked phases.

## At a glance

| | Item | Value |
|---|---|---|
| <img src="./assets/icons/gear.svg" width="24" alt=""/> | Shape | Seven parts (A–G), each with a goal, step card, and checkpoint |
| <img src="./assets/icons/activity-log.svg" width="24" alt=""/> | Hands-on time | 5–7 hours first build, 2–3 hours subsequent |
| <img src="./assets/icons/subscription.svg" width="24" alt=""/> | Elapsed time | 1–2 days first build, half a day subsequent |
| <img src="./assets/icons/policy.svg" width="24" alt=""/> | Result | A governed MCP gateway with a TLS-terminated, origin-locked runtime edge |

---

## Time budget

| | First build | Subsequent builds |
|---|---|---|
| **Hands-on time** | 5–7 hours | 2–3 hours |
| **Elapsed time** | 1–2 days | Half a day |

Elapsed exceeds hands-on because of waits you cannot compress: certificate issuance, quota increases, Entra role assignment propagation, and two long deployments.

### Front-load these — they have lead times measured in days

| | Blocker | Typical lead time | Start it on |
|---|---|---|---|
| <img src="./assets/icons/cost-management.svg" width="20" alt=""/> | Azure quota increase (APIM units, AKS cores, Foundry TPM) | 1–3 business days | Day 0, before anything else |
| <img src="./assets/icons/key-vault.svg" width="20" alt=""/> | TLS certificate issuance / DNS delegation | Hours to days | Day 0 |
| <img src="./assets/icons/app-registrations.svg" width="20" alt=""/> | Entra app registration approval (if you are not tenant admin) | Varies | Day 0 |
| <img src="./assets/icons/subscription.svg" width="20" alt=""/> | Subscription with Owner or Contributor + User Access Administrator | Varies | Day 0 |

> [!TIP]
> **If you do nothing else today, request quota and start the certificate.** Everything else is compressible; these are not.

---

## Prerequisites checklist

Confirm before Part A. Full detail in [02-prerequisites.md](./02-prerequisites.md).

| | Check | Why |
|---|---|---|
| <img src="./assets/icons/subscription.svg" width="20" alt=""/> | Azure subscription with **Owner**, or **Contributor + User Access Administrator** | Role assignments are part of the deployment |
| <img src="./assets/icons/entra-id.svg" width="20" alt=""/> | Target tenant id and subscription id **written down** (not "whatever `az` is currently pointed at") | Ambient `az` context drifts |
| <img src="./assets/icons/resource-group.svg" width="20" alt=""/> | Region verified against the availability matrix | API Management StandardV2 and API Center are the usual constraints |
| <img src="./assets/icons/cost-management.svg" width="20" alt=""/> | Quota confirmed: APIM units, AKS cores, Foundry model TPM, Cosmos RU, public IPs | Quota is the slowest blocker |
| <img src="./assets/icons/gear.svg" width="20" alt=""/> | Resource providers registered | Deployments fail otherwise |
| <img src="./assets/icons/app-registrations.svg" width="20" alt=""/> | Ability to create an Entra app registration **and assign app roles** | Both planes trust one app registration |
| <img src="./assets/icons/key-vault.svg" width="20" alt=""/> | TLS certificate available (or an issuance path) for the runtime edge | Part D adds the HTTPS listener |
| <img src="./assets/icons/powershell.svg" width="20" alt=""/> | Workstation tooling: `az`, `azd`, `kubectl`, an OCI builder, PowerShell 7, Python 3.11+, an MCP-capable client | Scripts and verification |
| <img src="./assets/icons/commit.svg" width="20" alt=""/> | Both upstream repositories cloned at the **pinned commits** | Reproducibility |

- [ ] All nine checks above are confirmed

---

## End state

[![Secure MCP Gateway architecture: consumers call API Management (governance plane), which forwards over TLS to Application Gateway and the MCP Gateway on AKS (runtime plane); Microsoft Entra ID validates the same token independently in both planes](./assets/secure-mcp-gateway-architecture.png)](./assets/secure-mcp-gateway-architecture.png)

<sub>Editable source: [`assets/secure-mcp-gateway-architecture.drawio`](./assets/secure-mcp-gateway-architecture.drawio) - regenerate with `python scripts/export_diagrams.py docs/assets`.</sub>

<details>
<summary><b>Text version of the end state (ASCII)</b></summary>

```
                        ┌──────────────────────────┐
                        │      MCP client          │
                        │ api-key + bearer token   │
                        └────────────┬─────────────┘
                                     │ Streamable HTTP
                                     ▼
╔══════════════════════ GOVERNANCE PLANE ══════════════════════╗
║   Azure API Management  (API type: mcp, streamable)          ║
║     ├── security-handler   → subscription key / JWT          ║
║     ├── mcp-usage          → inbound metering (FinOps)       ║
║     ├── product policy     → rate limit + quota per BU       ║
║     └── backend + circuit breaker  ── Authorization NOT      ║
║                                       rewritten (Mode B)     ║
║   Foundry · Event Hub · Logic App · Cosmos · Log Analytics   ║
╚═══════════════════════════════╤══════════════════════════════╝
                                │ HTTPS  (Part D adds TLS)
                                ▼
╔══════════════════════ MCP RUNTIME PLANE ═════════════════════╗
║   Application Gateway (TLS)                                  ║
║              │                                               ║
║   MCP Gateway service ── validates the SAME token            ║
║     ├── control plane   /adapters  /tools                    ║
║     ├── session routing → pins session → pod                 ║
║     └── data plane      reverse proxy                        ║
║              │  X-Mcp-UserId / X-Mcp-Roles                   ║
║              ▼                                               ║
║   Adapter StatefulSet (N replicas, headless Service)         ║
║     └── NetworkPolicy: reachable only via the gateway        ║
╚══════════════════════════════════════════════════════════════╝

           Microsoft Entra ID  ── validated independently
                                  by BOTH planes
```

</details>

---

## Part A — Ground truth and authentication *(~30 min)*

**Goal:** every later command targets the intended tenant, subscription, and commit.

| ☐ | Step | Action |
|---|---|---|
| ☐ | 1 | Write down `<TENANT_ID>`, `<SUBSCRIPTION_ID>`, `<AZURE_REGION>`. Do not proceed on assumption |
| ☐ | 2 | `az login --tenant <TENANT_ID>` → `az account set --subscription <SUBSCRIPTION_ID>` |
| ☐ | 3 | **Verify:** `az account show --query "{tenant:tenantId, subscription:id}" -o table` |
| ☐ | 4 | `azd auth login --tenant-id <TENANT_ID>` |
| ☐ | 5 | Clone both upstreams and `git checkout` the pinned SHAs from [README](../README.md#upstream-pins) |
| ☐ | 6 | `cp demo-ids.template.json demo-ids.local.json` and begin populating |

> [!WARNING]
> **Why this is Part A and not a footnote.** The Azure CLI keeps one ambient active account that changes whenever any other login happens anywhere on the machine. A bare `az login` will deploy into the wrong tenant without complaint. Verify, do not assume.

### ✅ Checkpoint A

- [ ] `az account show` reports the intended tenant **and** subscription
- [ ] `azd` authenticated to the same tenant
- [ ] Both repos at the pinned commits (`git rev-parse HEAD` matches)
- [ ] `demo-ids.local.json` exists and is **gitignored**

---

## Part B — Governance plane *(~45–60 min, mostly waiting)*

**Goal:** the AI front door exists, with metering and private networking.

→ [03-deployment.md Phase 1](./03-deployment.md#phase-1--deploy-the-governance-plane)

| ☐ | Step | Action |
|---|---|---|
| ☐ | 1 | `azd init --template Azure-Samples/ai-hub-gateway-solution-accelerator -e <ENV> --branch citadel-v1` |
| ☐ | 2 | Set explicitly: `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_LOCATION` |
| ☐ | 3 | Set the SKU: `APIM_SKU=Developer` for a lab, `StandardV2` otherwise |
| ☐ | 4 | `azd up` — **30–45 minutes** |
| ☐ | 5 | Capture outputs into `demo-ids.local.json` |

**Two traps.** Always set `AZURE_LOCATION` explicitly — the upstream default is inconsistent between sources. And the guides reference `main.parameters.dev.bicepparam`-style files that **do not exist** on this branch; tune with `azd env set` instead.

☕ **Good time to start the certificate request for Part D**, if you have not already.

### ✅ Checkpoint B

- [ ] `azd up` succeeded
- [ ] `GET https://<APIM_HOST>/version` returns the component manifest
- [ ] At least one Foundry model deployment succeeded
- [ ] Application Insights receiving telemetry
- [ ] Outputs captured

---

## Part C — Identity *(~30 min + propagation)*

**Goal:** one Entra app registration both planes trust.

→ [03-deployment.md Phase 2](./03-deployment.md#phase-2--entra-app-registration-and-app-roles)

| ☐ | Step | Action |
|---|---|---|
| ☐ | 1 | Create the app registration; record `<CLIENT_ID>` |
| ☐ | 2 | Set the identifier URI to `api://<CLIENT_ID>`; expose the delegated scope **`access`** |
| ☐ | 3 | Pre-authorize VS Code (`aebc6443-996d-45c2-90f0-388ff96faa56`) and Azure CLI (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) |
| ☐ | 4 | Create app roles `mcp.admin` and `mcp.engineer` |
| ☐ | 5 | **Assign them to real principals now** |
| ☐ | 6 | Verify: `az account get-access-token --tenant <TENANT_ID> --resource api://<CLIENT_ID>` |

> [!IMPORTANT]
> **Assign the roles now, not later.** Privileged capabilities fail closed to admin-only when unconfigured. A fresh environment where nobody holds a role returns `403` on everything, and it reads like a broken deployment rather than a missing grant.

### ✅ Checkpoint C

- [ ] `<CLIENT_ID>` recorded; identifier URI is `api://<CLIENT_ID>`
- [ ] Scope `access` exposed; both clients pre-authorized
- [ ] Both app roles exist; at least one human holds `mcp.admin`
- [ ] A token decodes locally with the expected `aud` and `roles`

---

## Part D — MCP runtime plane *(~60–90 min including TLS)*

**Goal:** a session-aware MCP runtime with a **TLS-terminated, origin-locked** edge.

→ [03-deployment.md Phase 3](./03-deployment.md#phase-3--deploy-the-mcp-runtime-plane) and [Phase 4](./03-deployment.md#phase-4--harden-the-mcp-runtime-edge)

| ☐ | Step | Action |
|---|---|---|
| ☐ | 1 | Re-verify tenant context — Parts B and C may have spanned a break |
| ☐ | 2 | `./deployment/Deploy-McpGateway.ps1 -ResourceGroupName … -ClientId <CLIENT_ID> -Location <REGION> -EnablePrivateEndpoints` |
| ☐ | 3 | `az aks get-credentials …`; confirm pods and NetworkPolicies |
| ☐ | 4 | Enumerate Cosmos containers; decide on the Agents/Sessions preview <img src="./assets/badges/preview.svg" alt="preview"/> |
| ☐ | 5 | **Add an HTTPS listener** with a real certificate; redirect HTTP → HTTPS |
| ☐ | 6 | **Lock the origin** so only API Management can reach it — and verify by attempting a direct call |
| ☐ | 7 | Set `GatewaySettings__Secret` to a strong random value |

> [!WARNING]
> **Part D is where demos go wrong.** The upstream deployment ships an **HTTP-only** listener. Point API Management at it unfixed and every bearer token crosses between planes in cleartext, and the origin stays reachable directly, bypassing quota and metering. Steps 5 and 6 are not optional hardening — they are load-bearing.

### ✅ Checkpoint D

- [ ] All pods `Running`; nodes `Ready`
- [ ] NetworkPolicies present in the `adapter` namespace
- [ ] `curl -sSI https://<MCP_ORIGIN>/healthz` returns `200` on a trusted chain
- [ ] HTTP redirects to HTTPS
- [ ] **A direct call bypassing API Management is blocked** *(verified, not assumed)*
- [ ] `GatewaySettings__Secret` is not a default or placeholder

---

## Part E — First MCP server *(~30 min)*

**Goal:** a real MCP server running with ≥2 replicas, proven **before** the gateway is involved.

→ [03-deployment.md Phase 5](./03-deployment.md#phase-5--build-and-register-an-mcp-server-adapter)

| ☐ | Step | Action |
|---|---|---|
| ☐ | 1 | `az acr build --registry <ACR> --image <ADAPTER>:1.0.0 ./samples/mcp-server` |
| ☐ | 2 | `./scripts/Register-McpAdapter.ps1 … -ReplicaCount 2 -RequiredRoles mcp.engineer` |
| ☐ | 3 | Confirm a **StatefulSet** and a **headless Service** — not a Deployment |
| ☐ | 4 | Validate **directly** against the runtime origin |
| ☐ | 5 | Call `describe_environment` repeatedly in one session — same pod every time |

**Use at least 2 replicas.** One replica cannot demonstrate session affinity, and session affinity is the reason this runtime is in the architecture.

**Test directly first.** If Part F later fails, you will already know the runtime is healthy and can look at the gateway instead.

### ✅ Checkpoint E

- [ ] Image present in the registry
- [ ] Adapter reports ready; expected replicas `Running`
- [ ] Workload is a StatefulSet with a headless Service
- [ ] Direct validation passes; both tools listed
- [ ] Session affinity observed directly

---

## Part F — Publish and grant *(~30 min)*

**Goal:** the MCP server becomes a governed, entitled endpoint.

→ [03-deployment.md Phase 6](./03-deployment.md#phase-6--publish-the-mcp-asset-through-api-management) and [Phase 7](./03-deployment.md#phase-7--grant-governed-access)
→ Deep dive: [06-mcp-publishing-and-access-contracts.md](./06-mcp-publishing-and-access-contracts.md)

| ☐ | Step | Action |
|---|---|---|
| ☐ | 1 | Fill in `infra/publish-contract/mcp-gateway.bicepparam` — `assetType: mcp-existing`, `transportType: streamable`, **`backend.url` https**, `backend.authType: none` |
| ☐ | 2 | `./scripts/Publish-McpAsset.ps1 …` — what-if, review, deploy |
| ☐ | 3 | Confirm the endpoint returns **`401`, not `404`**, without a key |
| ☐ | 4 | Resolve the published API name from the live instance (case-sensitive) |
| ☐ | 5 | Fill in `infra/access-contract/mcp-consumer.bicepparam`; deploy |
| ☐ | 6 | Retrieve the key from **Key Vault** |

> [!NOTE]
> **`401` is success here.** It means routing works and entitlement is enforced. A `404` almost always means the path shape is wrong — `mcp-existing` has **no** trailing `/mcp`.

### ✅ Checkpoint F

- [ ] An API of type `mcp` exists with `transportType: streamable`
- [ ] Backend points at the **HTTPS** origin; circuit breaker configured
- [ ] Unkeyed call returns `401`
- [ ] Product and subscription created with the expected naming
- [ ] Key retrieved from Key Vault (not from deployment outputs)

---

## Part G — Prove it *(~30 min)*

**Goal:** every control demonstrably engages. **Do not skip before a live demo.**

→ [04-testing.md](./04-testing.md)

| ☐ | Step | Action |
|---|---|---|
| ☐ | 1 | Full-path validation through the gateway |
| ☐ | 2 | **Negative tests** — no key `401`; over quota `429`; non-allow-listed tool `403`; wrong token audience rejected; direct-origin call blocked |
| ☐ | 3 | Session affinity through the gateway |
| ☐ | 4 | **Streaming integrity** — responses arrive incrementally |
| ☐ | 5 | Identity — `describe_environment` shows the **real caller**, not the gateway's identity |
| ☐ | 6 | Capture a regression baseline |

> [!TIP]
> **If you only run one test, run the streaming test.** A buffering policy is the most likely way to silently break this platform, and it presents as "the client hangs" rather than an error.

### ✅ Checkpoint G

- [ ] End-to-end validation passes through API Management
- [ ] **All** negative tests reject as expected
- [ ] Session affinity holds through the gateway
- [ ] Responses stream incrementally
- [ ] Forwarded identity is the real end user
- [ ] Baseline captured; `demo-ids.local.json` complete
- [ ] `git ls-files | grep -E '(secret|demo-ids\.json$|kubeconfig|\.env)'` returns nothing

---

## Compressed path — 90-minute lab

When you need something demonstrable today and are willing to accept an insecure result:

| Do | Skip | Cost of skipping |
|---|---|---|
| Parts A, C | — | Non-negotiable |
| Part B with `APIM_SKU=Developer` | Private endpoints | No SLA, public networking |
| Part D deploy | **TLS and origin lock** | ⚠️ **Cleartext tokens; gateway bypassable** |
| Part E with 1 replica | Session-affinity demo | Loses the main differentiator |
| Part F publish | Access Contract | No quota or chargeback |
| One smoke test | Everything else | Unverified controls |

**Never present a compressed build as representative.** Label it a lab, and never leave one running with an internet-reachable, HTTP-only origin.

---

## Teardown

```bash
azd down --purge                                              # governance plane
az group delete --name <MCP_PLANE_RG> --yes --no-wait          # runtime plane
az ad app delete --id <CLIENT_ID>                              # app registration
```

`--purge` matters — API Management, Key Vault, and Cognitive Services soft-delete and will block a same-name redeploy. The app registration outlives both resource groups; delete it explicitly.

---

## If you get stuck

| | Step | Action |
|---|---|---|
| <img src="./assets/icons/gear.svg" width="20" alt=""/> | 1. **Isolate the plane** | Test directly against the runtime origin. Works direct but fails through the gateway → governance plane or publish contract. Fails both → runtime |
| <img src="./assets/icons/dev-console.svg" width="20" alt=""/> | 2. **Check the quick-triage table** | See [05-troubleshooting.md](./05-troubleshooting.md) |
| <img src="./assets/icons/entra-id.svg" width="20" alt=""/> | 3. **Re-verify tenant context** | `az account show`. Ambient drift explains a surprising share of "it worked yesterday" |
| <img src="./assets/icons/azure-devops.svg" width="20" alt=""/> | 4. **Re-run `azd up`** | Transient provisioning failures are documented upstream and the deployment is idempotent |

---

Next: [01 Architecture →](./01-architecture.md)

*Last updated: 2026-10-02*
