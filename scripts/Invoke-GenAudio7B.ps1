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

$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'Invoke-GenAudio7B requires PowerShell 7 or newer.'
}
if (-not $WorkerUrl) {
    $environmentName = if ($Engine -eq 'vibevoice') {
        'GEN_AUDIO_VIBEVOICE_WORKER_URL'
    } else {
        'GEN_AUDIO_PERSONAPLEX_WORKER_URL'
    }
    $WorkerUrl = [Environment]::GetEnvironmentVariable($environmentName)
}
if (-not $WorkerUrl) {
    $port = if ($Engine -eq 'vibevoice') { 8996 } else { 8997 }
    $WorkerUrl = "http://127.0.0.1:$port"
}
$WorkerUrl = $WorkerUrl.TrimEnd('/')

$routes = @{
    Status = @{ Method = 'Get'; Path = '/v1/status' }
    Preflight = @{ Method = 'Get'; Path = '/v1/preflight' }
    Load = @{ Method = 'Post'; Path = '/v1/load' }
    Unload = @{ Method = 'Post'; Path = '/v1/unload' }
    Generate = @{ Method = 'Post'; Path = '/v1/generate' }
}
$route = $routes[$Action]
$body = $null
if ($Action -eq 'Generate') {
    if (-not $RequestPath) { throw 'Generate requires -RequestPath.' }
    if (-not (Test-Path -LiteralPath $RequestPath -PathType Leaf)) {
        throw "Request file not found: $RequestPath"
    }
    $body = Get-Content -LiteralPath $RequestPath -Raw
    $null = $body | ConvertFrom-Json
}

$parameters = @{
    Uri = "$WorkerUrl$($route.Path)"
    Method = $route.Method
    TimeoutSec = $TimeoutSeconds
}
if ($route.Method -eq 'Post') {
    $parameters.ContentType = 'application/json'
    $parameters.Body = if ($body) { $body } else { '{}' }
}
$receipt = Invoke-RestMethod @parameters
if ($Action -eq 'Generate' -and $OutputPath) {
    $source = [string]$receipt.output
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Worker returned a missing output path: $source"
    }
    $destination = [IO.Path]::GetFullPath($OutputPath)
    $parent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    if ($source -ne $destination) {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
    $receipt.output = $destination
}
$receipt | ConvertTo-Json -Depth 32

