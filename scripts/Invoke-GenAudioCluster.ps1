<#
.SYNOPSIS
Compatibility entry point for Windows-native cluster control.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Status', 'Preflight', 'Load', 'Unload', 'Generate')]
    [string]$Action,
    [ValidateSet('vibevoice', 'personaplex')]
    [string]$Engine,
    [string]$Node,
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'runtimes\cluster.windows.json'),
    [string]$RequestPath,
    [switch]$NoFallback,
    [int]$TimeoutSeconds = 1800
)
& (Join-Path $PSScriptRoot 'cluster.ps1') @PSBoundParameters
exit $LASTEXITCODE
