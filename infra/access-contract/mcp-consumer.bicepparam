// =============================================================================
// Access Contract — Secure MCP Gateway Landing Zone
// =============================================================================
//
// PURPOSE
//   Grants a consuming business unit governed access to the published MCP asset
//   (and, optionally, to LLM assets on the same gateway with the SAME key).
//
//   This is step 2 of 2. The Publish Contract
//   (../publish-contract/mcp-gateway.bicepparam) creates the endpoint; this file
//   creates the API Management product and subscription that entitle a consumer
//   to call it, along with the quota envelope and the single revocation point.
//
// WHAT THIS CREATES
//   - One API Management product per service code listed in `services`
//   - One subscription per product, with primary and secondary keys
//   - Optionally, Key Vault secrets holding the endpoint URL and key
//
// NAMING
//   Products follow  <code>-<businessUnit>-<useCaseName>-<environment>
//   Subscriptions follow the same stem with a -SUB-nn suffix.
//   When one contract spans multiple asset kinds (LLM + Tool/MCP), the
//   accelerator uses a MULTI- prefixed product so a single key covers all of
//   them under one quota envelope.
//
// HOW TO USE
//   1. Ensure the Publish Contract has already run and the MCP asset exists.
//   2. Copy this file over the accelerator's
//        bicep/infra/citadel-access-contracts/main.bicepparam
//   3. Replace every <PLACEHOLDER>.
//   4. Deploy:
//        az deployment sub create \
//          --name grant-mcp-access \
//          --location <AZURE_REGION> \
//          --template-file bicep/infra/citadel-access-contracts/main.bicep \
//          --parameters bicep/infra/citadel-access-contracts/main.bicepparam
//
// VERIFY BEFORE USE
//   The upstream schema is versioned (`access-contract-version` in the
//   accelerator's release.json). Diff against the accelerator's own
//   main.bicepparam at the pinned commit before deploying.
//
// Last updated: 2026-08-14
// =============================================================================

using 'main.bicep'

// -----------------------------------------------------------------------------
// REQUIRED — target API Management instance (the Governance Hub).
// -----------------------------------------------------------------------------
param apim = {
  subscriptionId: '<SUBSCRIPTION_ID>'
  resourceGroupName: '<GOVERNANCE_PLANE_RESOURCE_GROUP>'
  name: '<APIM_INSTANCE_NAME>'
}

// -----------------------------------------------------------------------------
// Store the generated endpoint URL and subscription key in Key Vault.
//
// STRONGLY RECOMMENDED: true for anything beyond a throwaway lab.
// When false, the subscription key is emitted in deployment OUTPUTS, which are
// readable by anyone with access to the deployment history and are captured in
// CI logs. Treat false as lab-only.
// -----------------------------------------------------------------------------
param useTargetAzureKeyVault = true

// -----------------------------------------------------------------------------
// REQUIRED even when useTargetAzureKeyVault is false.
// -----------------------------------------------------------------------------
param keyVault = {
  subscriptionId: '<SUBSCRIPTION_ID>'
  resourceGroupName: '<GOVERNANCE_PLANE_RESOURCE_GROUP>'
  name: '<KEY_VAULT_NAME>'
}

// -----------------------------------------------------------------------------
// REQUIRED — who this grant is for. Drives all naming.
//
// Keep useCaseName concise and space-free (PascalCase or kebab-case).
// One contract per consuming business unit per use case per environment — that
// granularity is what makes quota and revocation meaningful.
// -----------------------------------------------------------------------------
param useCase = {
  businessUnit: '<BUSINESS_UNIT>'      // e.g. 'PlatformEngineering'
  useCaseName: '<USE_CASE_NAME>'       // e.g. 'AgentToolAccess'
  environment: '<ENVIRONMENT>'         // DEV | TEST | UAT | STAGING | PROD
}

// -----------------------------------------------------------------------------
// REQUIRED — map service codes to the API Management API names they cover.
//
// API names are CASE-SENSITIVE and must already exist in API Management.
//
// The TOOL entry below is the MCP asset created by the Publish Contract. Its API
// name is derived from the `name` you set on the published asset — confirm the
// actual name before deploying:
//
//   az apim api list -g <GOVERNANCE_PLANE_RESOURCE_GROUP> \
//     -n <APIM_INSTANCE_NAME> --query "[].{name:name,path:path}" -o table
//
// Keep or remove the LLM entry depending on whether this consumer should get
// model access and tool access under ONE key. Listing both is what produces the
// combined MULTI- product described in docs/01-architecture.md.
// -----------------------------------------------------------------------------
param apiNameMapping = {
  TOOL: ['<PUBLISHED_MCP_API_NAME>']
  LLM: ['universal-llm-api', 'azure-openai-api']
}

// -----------------------------------------------------------------------------
// REQUIRED — the services this consumer is entitled to.
//
// Each entry creates a product + subscription. Remove the LLM entry if this
// consumer should have tool access only.
//
// policyXml is OPTIONAL. When omitted, the default product policy applies,
// which is where the accelerator attaches rate-limit-by-key and quota-by-key for
// Tool/Agent assets and llm-token-limit for LLM assets.
//
// ---------------------------------------------------------------------------
// MCP STREAMING CONSTRAINT — applies to any custom policyXml you attach here.
// MCP responses stream over SSE. A product policy that reads the response body
// buffers the stream and breaks the protocol. Keep any custom product policy
// INBOUND ONLY for products that include a TOOL/MCP service code.
// See docs/01-architecture.md "The streaming constraint".
// ---------------------------------------------------------------------------
// -----------------------------------------------------------------------------
param services = [
  {
    code: 'TOOL'
    endpointSecretName: 'MCP_TOOL_ENDPOINT'
    apiKeySecretName: 'MCP_TOOL_API_KEY'
    // policyXml: loadTextContent('policies/mcp-consumer-product-policy.xml')
  }
  {
    code: 'LLM'
    endpointSecretName: 'LLM_ENDPOINT'
    apiKeySecretName: 'LLM_API_KEY'
  }
]

// =============================================================================
// POST-DEPLOYMENT
// =============================================================================
//
// The consumer now needs TWO credentials to make a fully governed MCP call under
// this pattern's default identity mode:
//
//   1. The API Management subscription key from this contract
//        -> sent in the 'api-key' header
//        -> answers "which business unit is this, and what is their quota?"
//
//   2. An Entra bearer token for the MCP runtime's application ID URI
//        -> sent in the 'Authorization: Bearer' header
//        -> audience api://<MCP_GATEWAY_CLIENT_ID>
//        -> answers "which principal is this, and what may they reach?"
//
// The subscription key alone will authenticate at the gateway and then fail at
// the MCP runtime. The bearer token alone will be rejected at the gateway. Both
// are required by design — see docs/07-identity-and-token-flow.md.
//
// Validate the grant end to end:
//   python ../../scripts/validate_mcp_endpoint.py \
//     --endpoint https://<APIM_INSTANCE_NAME>.azure-api.net/mcp/<ASSET_PATH> \
//     --subscription-key <from Key Vault> \
//     --use-azure-identity --scope api://<MCP_GATEWAY_CLIENT_ID>/.default
// =============================================================================
