// =============================================================================
// Publish Contract — Secure MCP Gateway Landing Zone
// =============================================================================
//
// PURPOSE
//   Publishes the MCP runtime (microsoft/mcp-gateway) through the Citadel
//   Governance Hub's Azure API Management instance as a governed MCP asset.
//
// WHAT THIS DOES *NOT* DO
//   It does NOT grant anyone access. Publishing and entitlement are deliberately
//   separate steps. After this deploys, run the Access Contract
//   (see ../access-contract/mcp-consumer.bicepparam) to create the product and
//   subscription a consuming business unit actually uses.
//
// HOW TO USE
//   1. Clone the accelerator and check out the pinned commit:
//        git clone https://github.com/Azure-Samples/ai-hub-gateway-solution-accelerator.git
//        cd ai-hub-gateway-solution-accelerator
//        git checkout 06f7ea6c2f99371900feefba11c10212b16364b4
//   2. Copy this file over (or alongside) the accelerator's
//        bicep/infra/citadel-publish-contracts/main.bicepparam
//   3. Replace every <PLACEHOLDER> below.
//   4. Deploy with ../../scripts/Publish-McpAsset.ps1 (recommended — it enforces
//      tenant-explicit auth and runs a what-if first), or directly:
//
//        az deployment sub create \
//          --name publish-mcp-gateway \
//          --location <AZURE_REGION> \
//          --template-file bicep/infra/citadel-publish-contracts/main.bicep \
//          --parameters bicep/infra/citadel-publish-contracts/main.bicepparam
//
// VERIFY BEFORE USE
//   The upstream parameter schema is versioned (`publish-contract-version` in the
//   accelerator's release.json) and this subsystem is marked preview. Diff this
//   file against the accelerator's own main.bicepparam at the pinned commit
//   before deploying, and re-check after any upstream bump.
//
// Last updated: 2026-08-14
// =============================================================================

using 'main.bicep'

// -----------------------------------------------------------------------------
// Target API Management instance — must already exist with the Governance Hub
// deployed into it (docs/03-deployment.md Phase 1).
// -----------------------------------------------------------------------------
param apim = {
  subscriptionId: '<SUBSCRIPTION_ID>'
  resourceGroupName: '<GOVERNANCE_PLANE_RESOURCE_GROUP>'
  name: '<APIM_INSTANCE_NAME>'
}

// -----------------------------------------------------------------------------
// User-assigned managed identity client id used for backend authentication.
//
// IDENTITY MODE B (this pattern's default) uses backend authType 'none', so the
// caller's own Entra token passes through untouched and NO managed identity is
// required here. Leave this empty.
//
// Only populate this if you deliberately switch that asset to IDENTITY MODE A
// (backend authType 'managed-identity'), which replaces the caller's identity
// with API Management's. See docs/07-identity-and-token-flow.md before doing so.
// -----------------------------------------------------------------------------
param managedIdentityClientId = ''

// -----------------------------------------------------------------------------
// Circuit breaker — isolates MCP runtime failures from the shared AI gateway.
// Keep enabled. The same API Management instance also carries LLM traffic.
// -----------------------------------------------------------------------------
param configureCircuitBreaker = true

// -----------------------------------------------------------------------------
// Path prefixing. With this true (default), tool assets are served under
// {gateway}/mcp/... and agent assets under {gateway}/agent/....
// Keep true so MCP traffic is trivially identifiable in logs and policy scope.
// -----------------------------------------------------------------------------
param useAssetTypePathPrefix = true

// -----------------------------------------------------------------------------
// Azure API Center registration — the discovery catalog.
// Leave serviceName empty to disable registration entirely.
// -----------------------------------------------------------------------------
param apiCenter = {
  subscriptionId: ''          // defaults to apim.subscriptionId when empty
  resourceGroupName: ''       // defaults to apim.resourceGroupName when empty
  serviceName: '<API_CENTER_NAME_OR_EMPTY_TO_DISABLE>'
  workspaceName: 'default'
}

// =============================================================================
// ASSETS TO PUBLISH
// =============================================================================
param publishAssets = [
  // ---------------------------------------------------------------------------
  // The MCP runtime, published as a governed MCP asset.
  //
  // Resulting endpoint (useAssetTypePathPrefix = true, assetType = mcp-existing):
  //
  //     https://<APIM_INSTANCE_NAME>.azure-api.net/mcp/<path>
  //
  // NOTE THE PATH SHAPE. 'mcp-existing' does NOT get a trailing /mcp appended,
  // because the upstream runtime already serves MCP at its own path.
  // ('mcp-from-api' DOES get /mcp appended, because API Management synthesizes
  // the MCP surface itself.) Confusing the two is the most common 404 in this
  // architecture — see docs/05-troubleshooting.md.
  // ---------------------------------------------------------------------------
  {
    assetType: 'mcp-existing'
    name: '<ASSET_NAME>'                        // e.g. 'platform-tools'
    displayName: '<ASSET_DISPLAY_NAME>'         // e.g. 'Platform Tools (MCP)'
    description: 'Self-hosted MCP server fleet published and governed through the AI gateway.'

    path: '<ASSET_PATH>'                        // e.g. 'platform-tools'
    transportType: 'streamable'                 // the only transport both planes speak

    // API Management enforces a subscription key at the edge. This is what ties
    // a call to a consuming business unit and therefore to a quota envelope.
    subscriptionRequired: true

    // Header the caller sends the subscription key in.
    //   'api-key'                   -> matches the LLM surface on the same gateway
    //   'Ocp-Apim-Subscription-Key' -> API Management native default
    // Keep this consistent with your LLM assets so consumers use one convention.
    subscriptionKeyHeaderName: 'api-key'

    metadata: {
      version: '1.0.0'
      owner: '<OWNING_TEAM>'
      contactEmail: '<OWNER_CONTACT_EMAIL>'
      compliance: [ '<COMPLIANCE_TAG>' ]        // e.g. 'Internal'
      classification: '<CLASSIFICATION>'        // e.g. 'internal'
    }

    backend: {
      // The MCP runtime's TLS-terminated public origin (its Application Gateway).
      //
      // MUST be https. The upstream runtime ships an HTTP-only listener by
      // default — docs/03-deployment.md Phase 4 adds TLS. Publishing against http://
      // puts cleartext tool traffic on the wire between the two planes.
      url: 'https://<MCP_RUNTIME_PUBLIC_ORIGIN>'

      // ---------------------------------------------------------------------
      // IDENTITY MODE B — caller-token passthrough (this pattern's default).
      //
      // 'none' means API Management injects NO credential of its own, so the
      // caller's original Authorization: Bearer header reaches the MCP runtime
      // intact. The runtime validates it independently and derives the REAL
      // end-user identity, keeping its own RBAC meaningful.
      //
      // Allowed values upstream: none | managed-identity | api-key-header | api-key-bearer
      //
      // To switch to IDENTITY MODE A instead:
      //     authType: 'managed-identity'
      //     authConfig: { resource: 'api://<MCP_GATEWAY_CLIENT_ID>' }
      //   and set managedIdentityClientId above. Understand the confused-deputy
      //   trade-off first — docs/07-identity-and-token-flow.md.
      // ---------------------------------------------------------------------
      authType: 'none'

      // Per-asset circuit breaker override. Omit to accept the contract default
      // (3 failures within PT5M trips for PT1M, honoring Retry-After).
      // circuitBreaker: { failureCount: 3, failureInterval: 'PT5M', tripDuration: 'PT1M' }
    }

    // -------------------------------------------------------------------------
    // OPTIONAL — hardened policy overlay.
    //
    // Supplying policyXml REPLACES the baseline MCP policy for this asset, so the
    // overlay in ../policies/mcp-hardened-policy.xml deliberately begins with
    // <base /> to inherit the shared security and metering fragments.
    //
    // It adds tool-surface allow-listing and a per-asset rate-limit ceiling —
    // controls the MCP runtime does not provide today.
    //
    // Before enabling: create the 'mcp-allowed-tools' named value in API
    // Management and replace RATE_LIMIT_CALLS / RATE_LIMIT_PERIOD in the file.
    //
    // policyXml: loadTextContent('../../../policies/mcp-hardened-policy.xml')
    // -------------------------------------------------------------------------

    // Discovery catalog registration.
    publishToApiCenter: false
    apiCenter: {
      environmentName: '<APIC_ENVIRONMENT_NAME>'    // e.g. 'mcp-dev'
      lifecycleStage: 'development'
      customProperties: {
        Visibility: true
        Categories: [ 'AI/ML', 'Developer Tools' ]
        Vendor: 'Internal'
        Type: 'AI Gateway'
      }
    }
  }
]
