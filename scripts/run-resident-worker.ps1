[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Python,
    [Parameter(Mandatory)]
    [string]$ArgumentsBase64,
    [Parameter(Mandatory)]
    [string]$Stdout,
    [Parameter(Mandatory)]
    [string]$Stderr
)

$ErrorActionPreference = "Stop"
try {
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    Set-Location -LiteralPath $repoRoot
    $env:PYTHONPATH = Join-Path $repoRoot "src"
    $env:PYTHONFAULTHANDLER = "1"
    $env:PYTHONUNBUFFERED = "1"
    $json = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($ArgumentsBase64)
    )
    $arguments = @($json | ConvertFrom-Json)
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Python @arguments 1>> $Stdout 2>> $Stderr
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    exit $exitCode
} catch {
    ($_ | Out-String) | Add-Content -LiteralPath $Stderr -Encoding UTF8
    exit 1
}
