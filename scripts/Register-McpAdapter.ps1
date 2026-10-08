<#
.SYNOPSIS
    Registers (or idempotently re-registers) an MCP server as an adapter on the
    MCP Gateway control plane.

.DESCRIPTION
    Calls the MCP Gateway control-plane REST API to register a whole MCP server
    as an "adapter" (POST /adapters), then polls GET /adapters/{name} until the
    adapter is queryable (or a timeout elapses), and finally prints the client
    URL callers should use: {GatewayBaseUrl}/adapters/{Name}/mcp.

    Adapters run as Kubernetes StatefulSets in the `adapter` namespace. The
    container image named by -ImageName/-ImageVersion must already exist in the
    ACR the MCP Gateway is configured to pull from -- this script does not
    build or push images (see samples/mcp-server/Dockerfile's `az acr build`
    comment for that step).

    Authentication is tenant-explicit: this workstation is signed into multiple
    Azure tenants and the ambient `az` CLI context silently drifts between
    them, so this script never trusts it blindly. Unless -Token is supplied
    directly, it verifies (via Test-AzTenantContext) that the active `az`
    session actually matches -TenantId before acquiring a token with
    `az account get-access-token --tenant <TenantId> --resource <scope>`, and
    fails loudly with remediation guidance if it does not.

    The control-plane contract exposes no PATCH/update endpoint for adapters
    (only POST /adapters, GET /adapters, GET /adapters/{name}, DELETE
    /adapters/{name}, GET /adapters/{name}/logs). Idempotency is therefore:
    if the adapter already exists, report and skip; with -Force, DELETE the
    existing adapter first and re-register from scratch.

.PARAMETER GatewayBaseUrl
    Base URL of the MCP Gateway control plane, e.g. https://<mcp-gateway-host>.
    No trailing slash.

.PARAMETER Name
    The adapter name to register the MCP server under (used in
    /adapters/{name} paths and as the StatefulSet name in the `adapter`
    namespace).

.PARAMETER ImageName
    Fully qualified container image name (e.g.
    <acr-name>.azurecr.io/mcp-samples/mcp-sample-server). Must already exist
    in the ACR the MCP Gateway is configured to pull from.

.PARAMETER ImageVersion
    Image tag/version to deploy. Defaults to 'latest'.

.PARAMETER Description
    Free-text description of the adapter, stored on the control-plane record.

.PARAMETER ReplicaCount
    Number of adapter replicas to run. Defaults to 2. NOTE: any value greater
    than 1 depends entirely on MCP Gateway's session-affinity layer
    (mcp-session-id header / session_id query param) to pin a caller to the
    same replica across a multi-call session -- that affinity behavior is the
    whole point of running more than one replica for a stateful adapter, not
    an incidental detail.

.PARAMETER RequiredRoles
    Array of Entra app roles a caller must hold to reach this adapter (e.g.
    'mcp.engineer'). Maps to the control-plane `requiredRoles` field.

.PARAMETER UseWorkloadIdentity
    Switch. When set, the adapter authenticates to downstream Azure resources
    via workload identity instead of adapter-supplied credentials. Maps to the
    control-plane `useWorkloadIdentity` field.

.PARAMETER EnvironmentVariables
    Hashtable of environment variables to inject into the adapter's pod. Maps
    to the control-plane `environmentVariables` object.

.PARAMETER TenantId
    Entra tenant ID the caller's `az` CLI session (and the acquired token)
    must belong to. Required unless -Token is supplied directly.

.PARAMETER ClientId
    Client (application) ID of the MCP Gateway app registration. Used to build
    a default -Scope of "api://<ClientId>" when -Scope is not supplied.

.PARAMETER Scope
    Explicit token resource/scope to request, e.g. api://<client-id>. Passed
    verbatim as `az account get-access-token --resource <Scope>`. If omitted,
    derived from -ClientId.

.PARAMETER Token
    A raw bearer token to use instead of acquiring one via `az`. When supplied,
    -TenantId/-ClientId/-Scope are not required and no `az` calls are made for
    authentication. Never written to output or disk.

.PARAMETER ReadyTimeoutSeconds
    Maximum time to poll GET /adapters/{name} after registration before giving
    up and warning (registration itself is not undone). Defaults to 180.

.PARAMETER PollIntervalSeconds
    Delay between readiness polls. Defaults to 5.

.PARAMETER Force
    If the adapter already exists, delete it first and re-register from
    scratch (see DESCRIPTION for why this is a delete+recreate rather than an
    update -- there is no verified PATCH endpoint).

.EXAMPLE
    ./Register-McpAdapter.ps1 `
        -GatewayBaseUrl 'https://mcp-gateway.example.internal' `
        -Name 'mcp-sample-server' `
        -ImageName '<acr-name>.azurecr.io/mcp-samples/mcp-sample-server' `
        -ImageVersion '1.0.0' `
        -ReplicaCount 2 `
        -RequiredRoles @('mcp.engineer') `
        -TenantId '<TENANT_ID>' `
        -ClientId '<CLIENT_ID>'

    Registers the sample MCP server as an adapter, acquiring a token via `az`
    for tenant <TENANT_ID> and scope "api://<CLIENT_ID>".

.EXAMPLE
    ./Register-McpAdapter.ps1 `
        -GatewayBaseUrl 'https://mcp-gateway.example.internal' `
        -Name 'mcp-sample-server' `
        -ImageName '<acr-name>.azurecr.io/mcp-samples/mcp-sample-server' `
        -Token $env:MCP_GATEWAY_TOKEN `
        -Force `
        -WhatIf

    Previews (without side effects, via -WhatIf) deleting and re-registering
    an existing adapter using a pre-acquired bearer token instead of
    acquiring one via `az`.

.NOTES
    - Requires the Azure CLI (`az`) on PATH unless -Token is supplied.
    - Requires PowerShell 7+.
    - Never writes the bearer token to console output, verbose/debug streams,
      or disk.
    - Only the verified MCP Gateway control-plane endpoints are used:
      POST /adapters, GET /adapters/{name}, DELETE /adapters/{name}.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$GatewayBaseUrl,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$Name,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$ImageName,

    [string]$ImageVersion = 'latest',

    [string]$Description = '',

    # NOTE: ReplicaCount > 1 relies entirely on MCP Gateway's session-affinity
    # layer (mcp-session-id header / session_id query param) to pin a caller to
    # the same replica across a session -- that affinity behavior is the whole
    # point of running more than one replica for a stateful adapter.
    [ValidateRange(1, 50)]
    [int]$ReplicaCount = 2,

    [string[]]$RequiredRoles = @(),

    [switch]$UseWorkloadIdentity,

    [hashtable]$EnvironmentVariables = @{},

    [string]$TenantId,

    [string]$ClientId,

    [string]$Scope,

    [string]$Token,

    [int]$ReadyTimeoutSeconds = 180,

    [int]$PollIntervalSeconds = 5,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Bounds every MCP Gateway HTTP call (including the idempotency-check GET,
# which runs unconditionally -- even under -WhatIf -- before the
# ShouldProcess gate). Invoke-RestMethod has no client-side timeout by
# default, so an unresponsive/black-holed -GatewayBaseUrl would otherwise
# hang this script (and a -WhatIf preview) indefinitely instead of failing
# fast.
$script:McpRequestTimeoutSeconds = 30

# Every Invoke-RestMethod call below also passes -Verbose:$false -Debug:$false
# explicitly. This is a deliberate secret-leakage guard, not stylistic: the
# WebRequestPSCmdlet's own built-in "WebRequest Detail" debug trace dumps every
# outgoing request header verbatim -- including the bearer-token Authorization
# header -- whenever $DebugPreference/-Debug is enabled by the *caller*
# (interactively, in a transcript, or in a CI log). An explicit per-call
# -Debug:$false override suppresses that inherited framework-level trace
# regardless of ambient preference, without touching this script's own
# deliberate Write-Verbose/-Verbose support.

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Test-AzTenantContext {
    <#
    .SYNOPSIS
        Verifies the active `az` CLI context matches the expected tenant, and
        throws a loud, actionable error if it does not.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$TenantId
    )

    $raw = az account show --output json 2>$null
    if (-not $raw) {
        throw "Unable to read the active az CLI context. Run 'az login --tenant $TenantId' first, then retry."
    }

    $account = $raw | ConvertFrom-Json
    if ($account.tenantId -ne $TenantId) {
        throw @"
Active az CLI context is tenant '$($account.tenantId)' ($($account.name)), but
this operation targets tenant '$TenantId'.

This workstation is signed into multiple Azure tenants and the ambient az CLI
context silently drifts between them -- never assume the active account is
correct. Re-authenticate against the correct tenant before retrying:

    az login --tenant $TenantId

Then re-run this script.
"@
    }

    Write-Verbose "az CLI context verified: tenant '$TenantId' ($($account.name))."
    return $account
}

function Invoke-McpGatewayRequest {
    <#
    .SYNOPSIS
        Thin Invoke-RestMethod wrapper that surfaces the response body on
        non-2xx failures instead of swallowing it.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Method,

        [Parameter(Mandatory)]
        [string]$Uri,

        [Parameter(Mandatory)]
        [hashtable]$Headers,

        [object]$Body
    )

    try {
        if ($null -ne $Body) {
            $json = $Body | ConvertTo-Json -Depth 10
            return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers -Body $json -ContentType 'application/json' -TimeoutSec $script:McpRequestTimeoutSeconds -Verbose:$false -Debug:$false -ErrorAction Stop
        }
        return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers -TimeoutSec $script:McpRequestTimeoutSeconds -Verbose:$false -Debug:$false -ErrorAction Stop
    } catch {
        $responseBody = $null
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $responseBody = $_.ErrorDetails.Message
        }
        $statusCode = $null
        if ($_.Exception.PSObject.Properties.Name -contains 'Response' -and $_.Exception.Response) {
            try { $statusCode = [int]$_.Exception.Response.StatusCode } catch { $statusCode = $null }
        }

        $message = "MCP Gateway request failed: $Method $Uri"
        if ($statusCode) { $message += " (HTTP $statusCode)" }
        if ($responseBody) { $message += "`nResponse body: $responseBody" }
        throw $message
    }
}

function Get-McpAdapter {
    <#
    .SYNOPSIS
        Returns the adapter record if it exists, or $null (never throws for a
        plain not-found response).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$GatewayBaseUrl,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][hashtable]$Headers
    )
    try {
        return Invoke-RestMethod -Method Get -Uri "$GatewayBaseUrl/adapters/$Name" -Headers $Headers -TimeoutSec $script:McpRequestTimeoutSeconds -Verbose:$false -Debug:$false -ErrorAction Stop
    } catch {
        return $null
    }
}

# ---------------------------------------------------------------------------
# Resolve the bearer token (tenant-explicit; never logged/written to disk)
# ---------------------------------------------------------------------------

if (-not $Token) {
    if (-not $TenantId) {
        throw "Either -Token or -TenantId (for az CLI token acquisition) must be supplied."
    }

    Test-AzTenantContext -TenantId $TenantId | Out-Null

    $resolvedScope = $Scope
    if (-not $resolvedScope) {
        if ($ClientId) {
            $resolvedScope = "api://$ClientId"
        } else {
            throw "Either -Scope or -ClientId is required to acquire a token via az CLI (or supply -Token directly)."
        }
    }

    Write-Verbose "Acquiring access token for resource '$resolvedScope' in tenant '$TenantId' via az CLI."
    $Token = az account get-access-token --tenant $TenantId --resource $resolvedScope --query accessToken -o tsv 2>$null
    if ([string]::IsNullOrWhiteSpace($Token)) {
        throw "Failed to acquire an access token via 'az account get-access-token --tenant $TenantId --resource $resolvedScope'. Run 'az login --tenant $TenantId' and retry."
    }
}

$headers = @{
    Authorization = "Bearer $Token"
    Accept        = 'application/json'
}

# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------

$clientUrl = "$GatewayBaseUrl/adapters/$Name/mcp"
$existing = Get-McpAdapter -GatewayBaseUrl $GatewayBaseUrl -Name $Name -Headers $headers

if ($existing -and -not $Force) {
    Write-Host "Adapter '$Name' already exists on $GatewayBaseUrl -- skipping (use -Force to delete and re-register)."
    Write-Host "Client URL: $clientUrl"
    return
}

if ($existing -and $Force) {
    if ($PSCmdlet.ShouldProcess($Name, "Delete existing adapter before re-registering (no update/PATCH endpoint is exposed)")) {
        Write-Verbose "Deleting existing adapter '$Name' prior to re-registration."
        Invoke-McpGatewayRequest -Method Delete -Uri "$GatewayBaseUrl/adapters/$Name" -Headers $headers | Out-Null
    }
}

# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

$body = [ordered]@{
    name                 = $Name
    imageName            = $ImageName
    imageVersion         = $ImageVersion
    description          = $Description
    replicaCount         = $ReplicaCount
    environmentVariables = $EnvironmentVariables
    requiredRoles        = @($RequiredRoles)
    useWorkloadIdentity  = [bool]$UseWorkloadIdentity
}

if ($PSCmdlet.ShouldProcess($Name, "Register MCP adapter on $GatewayBaseUrl")) {
    Invoke-McpGatewayRequest -Method Post -Uri "$GatewayBaseUrl/adapters" -Headers $headers -Body $body | Out-Null
    Write-Host "Adapter '$Name' registered on $GatewayBaseUrl."

    # -------------------------------------------------------------------
    # Poll until ready or timeout.
    #
    # The control-plane contract does not specify a readiness field schema
    # for GET /adapters/{name} (only that the endpoint exists), so "ready"
    # here means "the adapter document is retrievable via GET" -- the
    # strongest readiness signal available without assuming an unverified
    # field. Any status-shaped fields the response happens to include are
    # surfaced for visibility, without hard-depending on them.
    # -------------------------------------------------------------------
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    $ready = $false
    $probe = $null
    do {
        Start-Sleep -Seconds $PollIntervalSeconds
        $probe = Get-McpAdapter -GatewayBaseUrl $GatewayBaseUrl -Name $Name -Headers $headers
        $ready = $null -ne $probe
    } while (-not $ready -and (Get-Date) -lt $deadline)

    if (-not $ready) {
        Write-Warning "Timed out after $ReadyTimeoutSeconds second(s) waiting for adapter '$Name' to become queryable via GET /adapters/$Name."
    } else {
        Write-Host "Adapter '$Name' is queryable via GET /adapters/$Name."
        foreach ($prop in @('status', 'state', 'ready', 'readyReplicas')) {
            if ($probe.PSObject.Properties.Name -contains $prop) {
                Write-Host "  $prop`: $($probe.$prop)"
            }
        }
    }

    Write-Host "Client URL: $clientUrl"
} else {
    Write-Host "WhatIf: would register adapter '$Name' on $GatewayBaseUrl and poll for readiness."
    Write-Host "Client URL (once registered): $clientUrl"
}
