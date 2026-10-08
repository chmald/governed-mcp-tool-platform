<#
.SYNOPSIS
    Deploys the Citadel Governance Hub "Publish Contract" that exposes the MCP
    Gateway through Azure API Management as a native `type: 'mcp'` API.

.DESCRIPTION
    Wires Plane 2 (the MCP Gateway on AKS) behind Plane 1 (the Citadel
    Governance Hub's APIM instance) by deploying the accelerator's
    Publish-Contract Bicep template with `assetType: 'mcp-existing'` at
    subscription scope.

    This script never trusts the ambient `az` CLI context. This workstation is
    signed into multiple Azure tenants and the active account silently drifts
    between them, so Phase 0 always re-verifies (via Test-AzTenantContext)
    that the active tenant and subscription match -TenantId/-SubscriptionId
    before any deployment operation, hard-failing fast on any mismatch --
    and hard-failing fast (never invoking `az login` itself) when no active
    session exists at all, so an operator can never be silently dropped into
    a blocking interactive browser/device-code login from within this
    script, including under -WhatIfOnly.

    Before deploying, the script checks that the local clone of
    Azure-Samples/ai-hub-gateway-solution-accelerator (branch citadel-v1) at
    -AcceleratorPath is pinned to the expected commit SHA
    06f7ea6c2f99371900feefba11c10212b16364b4. A mismatch is a WARNING, not a
    hard failure, since a newer pinned commit may still be a valid deploy
    target -- but the operator should know before proceeding.

    Deployment is subscription-scoped (`az deployment sub ...`). A
    `what-if` preview always runs first and its output is printed. With
    -WhatIfOnly, the script stops after the preview. Otherwise, actually
    creating the deployment is gated behind PowerShell's own ShouldProcess/
    -Confirm mechanism (this script's ConfirmImpact is High, so by default
    the caller is prompted to confirm before `az deployment sub create` runs).

    On success, the script reads the deployment's outputs and prints the
    published MCP endpoint URL. The accelerator's exact output name for this
    value is not part of the verified fact set this pattern was built from,
    so the script searches the outputs for a value that looks like an HTTPS
    URL (preferring an output key that mentions "endpoint", "gateway", or
    "url") rather than assuming a specific hardcoded output name; if nothing
    confidently matches, it prints the full outputs object so the operator
    can locate the value themselves.

.PARAMETER TenantId
    Entra tenant ID that must be active in the `az` CLI context before any
    deployment operation runs.

.PARAMETER SubscriptionId
    Subscription ID the deployment targets. Verified against the active `az`
    context after tenant login.

.PARAMETER Location
    Azure region passed to `az deployment sub what-if`/`create` (subscription-
    scoped deployments require a deployment location distinct from any
    resource-group location).

.PARAMETER AcceleratorPath
    Path to the local clone of Azure-Samples/ai-hub-gateway-solution-
    accelerator (branch citadel-v1). Used both to resolve the default
    -TemplateFile and to check the pinned commit SHA.

.PARAMETER ParameterFile
    Path to the Bicep parameters file for the publish-contract deployment.
    Defaults to '<repo>/infra/publish-contract/mcp-gateway.bicepparam'
    (relative to this script's location).

.PARAMETER TemplateFile
    Path to the publish-contract Bicep entry template. Defaults to
    'bicep/infra/citadel-publish-contracts/main.bicep' inside
    -AcceleratorPath.

.PARAMETER DeploymentName
    Name for the `az deployment sub` operation. Defaults to a timestamped
    'mcp-gateway-publish-<yyyyMMddHHmmss>' name when not supplied.

.PARAMETER WhatIfOnly
    Switch. When set, run `az deployment sub what-if` and print its output,
    then stop -- never calls `az deployment sub create`.

.EXAMPLE
    ./Publish-McpAsset.ps1 `
        -TenantId '<TENANT_ID>' `
        -SubscriptionId '<SUBSCRIPTION_ID>' `
        -Location 'eastus2' `
        -AcceleratorPath 'C:\src\ai-hub-gateway-solution-accelerator' `
        -WhatIfOnly

    Verifies tenant/subscription context, checks the accelerator's pinned
    commit, and prints a what-if preview of the publish-contract deployment
    without creating anything.

.EXAMPLE
    ./Publish-McpAsset.ps1 `
        -TenantId '<TENANT_ID>' `
        -SubscriptionId '<SUBSCRIPTION_ID>' `
        -Location 'eastus2' `
        -AcceleratorPath 'C:\src\ai-hub-gateway-solution-accelerator' `
        -ParameterFile '..\infra\publish-contract\mcp-gateway.bicepparam'

    Runs the what-if preview, then (after the operator confirms the
    ShouldProcess prompt this script's High ConfirmImpact triggers by
    default) creates the subscription-scoped deployment and prints the
    published MCP endpoint URL.

.NOTES
    - Requires the Azure CLI (`az`) with the `bicep` component available on
      PATH.
    - Requires PowerShell 7+.
    - Deployment is subscription-scoped: `az deployment sub what-if` /
      `az deployment sub create`.
    - Never writes secrets to output or disk; this script only handles
      tenant/subscription identifiers and file paths, no tokens or keys.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$TenantId,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$SubscriptionId,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$Location,

    [string]$AcceleratorPath = (Join-Path $PSScriptRoot '..\..\ai-hub-gateway-solution-accelerator'),

    [string]$ParameterFile = (Join-Path $PSScriptRoot '..\infra\publish-contract\mcp-gateway.bicepparam'),

    [string]$TemplateFile,

    [string]$DeploymentName = "mcp-gateway-publish-$(Get-Date -Format 'yyyyMMddHHmmss')",

    [switch]$WhatIfOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Expected pinned commit for Azure-Samples/ai-hub-gateway-solution-accelerator
# (branch citadel-v1). A mismatch is a warning, not a hard failure.
$script:ExpectedAcceleratorCommit = '06f7ea6c2f99371900feefba11c10212b16364b4'

if (-not $TemplateFile) {
    $TemplateFile = Join-Path $AcceleratorPath 'bicep\infra\citadel-publish-contracts\main.bicep'
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Test-AzTenantContext {
    <#
    .SYNOPSIS
        Verifies the active `az` CLI context matches the expected tenant and
        subscription, throwing a loud, actionable error if it does not.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$TenantId,

        [string]$SubscriptionId
    )

    $raw = az account show --output json 2>$null
    if (-not $raw) {
        return $null
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

    if ($SubscriptionId -and $account.id -ne $SubscriptionId) {
        throw @"
Active az CLI subscription is '$($account.id)' ($($account.name)), but this
operation targets subscription '$SubscriptionId' (tenant '$TenantId' matches).

Set the correct subscription before retrying:

    az account set --subscription $SubscriptionId

Then re-run this script.
"@
    }

    Write-Verbose "az CLI context verified: tenant '$TenantId', subscription '$($account.id)'."
    return $account
}

function Get-AcceleratorCommitSha {
    <#
    .SYNOPSIS
        Returns the HEAD commit SHA of the local accelerator clone, or $null
        if it cannot be determined (e.g. not a git repo, git unavailable).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $null
    }

    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    if (-not $gitCmd) {
        Write-Warning "git is not on PATH -- cannot verify the accelerator clone's pinned commit."
        return $null
    }

    Push-Location -LiteralPath $Path
    try {
        $sha = git rev-parse HEAD 2>$null
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        return $sha.Trim()
    } finally {
        Pop-Location
    }
}

function Find-PublishedEndpointUrl {
    <#
    .SYNOPSIS
        Best-effort extraction of the published MCP endpoint URL from a
        deployment outputs object. The accelerator's exact output name for
        this value is not a verified fact, so this searches by shape
        (an https URL) and prefers keys that look endpoint/gateway/url-ish.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [psobject]$Outputs
    )

    if (-not $Outputs) {
        return $null
    }

    $candidates = @()
    foreach ($prop in $Outputs.PSObject.Properties) {
        $value = $prop.Value.value
        if ($value -is [string] -and $value -match '^https://') {
            $candidates += [pscustomobject]@{
                Key   = $prop.Name
                Value = $value
            }
        }
    }

    if ($candidates.Count -eq 0) {
        return $null
    }

    $preferred = $candidates | Where-Object { $_.Key -match '(?i)(endpoint|gateway|mcp|url)' }
    if ($preferred) {
        return $preferred[0].Value
    }

    return $candidates[0].Value
}

# ---------------------------------------------------------------------------
# Phase 0 -- tenant/subscription authentication (mandatory, never ambient)
# ---------------------------------------------------------------------------

Write-Host "Phase 0: verifying Azure CLI tenant/subscription context..."

$account = Test-AzTenantContext -TenantId $TenantId -SubscriptionId $SubscriptionId
if (-not $account) {
    # Deliberately fail fast instead of invoking `az login` ourselves: an
    # automatic login attempt here would block indefinitely on interactive
    # browser/device-code authentication -- including under -WhatIfOnly,
    # where the caller only asked for a preview. Never trust or silently
    # remediate ambient auth state; make the operator authenticate
    # explicitly, then retry.
    throw "Unable to read the active az CLI context (no active session detected). Run 'az login --tenant $TenantId' first, then retry."
}

az account set --subscription $SubscriptionId | Out-Null
$account = Test-AzTenantContext -TenantId $TenantId -SubscriptionId $SubscriptionId
if (-not $account) {
    throw "Failed to verify az CLI context for tenant '$TenantId' / subscription '$SubscriptionId' after 'az account set'."
}

Write-Host "Confirmed: tenant '$TenantId', subscription '$SubscriptionId' ($($account.name))."

# ---------------------------------------------------------------------------
# Accelerator clone + pinned-commit check (warn only)
# ---------------------------------------------------------------------------

if (-not (Test-Path -LiteralPath $AcceleratorPath -PathType Container)) {
    throw "Accelerator clone not found at '$AcceleratorPath'. Clone Azure-Samples/ai-hub-gateway-solution-accelerator (branch citadel-v1) there, or pass -AcceleratorPath."
}

$actualCommit = Get-AcceleratorCommitSha -Path $AcceleratorPath
if ($actualCommit) {
    if ($actualCommit -eq $script:ExpectedAcceleratorCommit) {
        Write-Host "Accelerator clone is pinned to the expected commit ($($script:ExpectedAcceleratorCommit))."
    } else {
        Write-Warning "Accelerator clone at '$AcceleratorPath' is at commit '$actualCommit', not the expected pinned commit '$($script:ExpectedAcceleratorCommit)'. Continuing, but verify this is intentional."
    }
} else {
    Write-Warning "Could not determine the accelerator clone's commit SHA -- skipping pinned-commit verification."
}

if (-not (Test-Path -LiteralPath $TemplateFile -PathType Leaf)) {
    throw "Publish-contract template not found at '$TemplateFile'. Pass -TemplateFile or -AcceleratorPath explicitly."
}

# ---------------------------------------------------------------------------
# What-if preview (always runs; never mutates)
# ---------------------------------------------------------------------------

Write-Host "`nRunning what-if preview of the publish-contract deployment..."

$whatIfArgs = @(
    'deployment', 'sub', 'what-if',
    '--name', $DeploymentName,
    '--location', $Location,
    '--template-file', $TemplateFile,
    '--subscription', $SubscriptionId
)
if (Test-Path -LiteralPath $ParameterFile -PathType Leaf) {
    $whatIfArgs += @('--parameters', $ParameterFile)
} else {
    Write-Warning "Parameter file '$ParameterFile' not found -- running what-if without --parameters."
}

az @whatIfArgs
if ($LASTEXITCODE -ne 0) {
    throw "az deployment sub what-if failed with exit code $LASTEXITCODE."
}

if ($WhatIfOnly) {
    Write-Host "`n-WhatIfOnly supplied -- stopping after the what-if preview. No deployment was created."
    return
}

# ---------------------------------------------------------------------------
# Actual deployment (gated behind ShouldProcess; ConfirmImpact = High means
# the caller is prompted by default unless $ConfirmPreference has been
# lowered, or -Confirm:$false is passed explicitly).
# ---------------------------------------------------------------------------

$target = "subscription '$SubscriptionId' (tenant '$TenantId', location '$Location')"
if ($PSCmdlet.ShouldProcess($target, "az deployment sub create ($DeploymentName) publishing the MCP Gateway through APIM")) {
    Write-Host "`nCreating the publish-contract deployment..."

    $createArgs = @(
        'deployment', 'sub', 'create',
        '--name', $DeploymentName,
        '--location', $Location,
        '--template-file', $TemplateFile,
        '--subscription', $SubscriptionId,
        '--output', 'json'
    )
    if (Test-Path -LiteralPath $ParameterFile -PathType Leaf) {
        $createArgs += @('--parameters', $ParameterFile)
    }

    $deploymentJson = az @createArgs
    if ($LASTEXITCODE -ne 0) {
        throw "az deployment sub create failed with exit code $LASTEXITCODE."
    }

    $deployment = $deploymentJson | ConvertFrom-Json
    Write-Host "Deployment '$DeploymentName' succeeded (provisioningState: $($deployment.properties.provisioningState))."

    $endpointUrl = Find-PublishedEndpointUrl -Outputs $deployment.properties.outputs
    if ($endpointUrl) {
        Write-Host "`nPublished MCP endpoint URL: $endpointUrl"
    } else {
        Write-Warning "Could not confidently identify the published endpoint URL from deployment outputs. Full outputs follow:"
        $deployment.properties.outputs | ConvertTo-Json -Depth 10 | Write-Host
    }
} else {
    Write-Host "`nSkipped: az deployment sub create was not confirmed."
}
