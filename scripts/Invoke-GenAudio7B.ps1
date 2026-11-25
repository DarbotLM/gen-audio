<#
.SYNOPSIS
Compatibility entry point for the Windows-native model worker client.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('vibevoice', 'personaplex')]
    [string]$Engine,
    [Parameter(Mandatory)]
    [ValidateSet('Status', 'Preflight', 'Load', 'Unload', 'Generate')]
    [string]$Action,
    [string]$WorkerUrl,
    [string]$RequestPath,
    [string]$OutputPath,
    [int]$TimeoutSeconds = 1200
)
& (Join-Path $PSScriptRoot 'model_clients.ps1') @PSBoundParameters
exit $LASTEXITCODE
