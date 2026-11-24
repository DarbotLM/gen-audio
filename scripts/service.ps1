#Requires -Version 7.0
<#
.SYNOPSIS
Own the Windows-native Gen-Audio worker service boundary.

.DESCRIPTION
Starts, stops, or probes the wingen.audio public worker server. The native host
validates the /v1 wire contract and supervises the loopback ML backend. This
script owns Windows PID state, port ownership, logs, restart, and cleanup.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('vibevoice', 'personaplex')]
    [string]$Engine,
    [Parameter(Mandatory)]
    [ValidateSet('Start', 'Stop', 'Status', 'Restart')]
    [string]$Action,
    [ValidateRange(1, 65535)]
    [int]$Port,
    [ValidateRange(1, 65535)]
    [int]$BackendPort,
    [ValidateRange(1, 7200)]
    [int]$TimeoutSeconds = 1800,
    [string]$NativeHost = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$serviceInvocation = @{
    Engine = $Engine
    Action = $Action
    Port = $Port
    BackendPort = $BackendPort
    TimeoutSeconds = $TimeoutSeconds
    NativeHost = $NativeHost
}
. (Join-Path $PSScriptRoot 'model_clients.ps1')
$Engine = $serviceInvocation.Engine
$Action = $serviceInvocation.Action
$Port = $serviceInvocation.Port
$BackendPort = $serviceInvocation.BackendPort
$TimeoutSeconds = $serviceInvocation.TimeoutSeconds
$NativeHost = $serviceInvocation.NativeHost

if (-not $Port) { $Port = if ($Engine -eq 'vibevoice') { 8996 } else { 8997 } }
if (-not $BackendPort) { $BackendPort = $Port + 10000 }
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'artifacts\runtime\7b'
$pidPath = Join-Path $runtime "$Engine.control.pid"
$stdout = Join-Path $runtime "$Engine.control.stdout.log"
$stderr = Join-Path $runtime "$Engine.control.stderr.log"
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$native = Resolve-WingenAudioNativeHost -ExplicitPath $NativeHost
if (-not $native) { throw 'wingen.audio.exe is not published. Build src\wingen.audio\wingen.audio.csproj first.' }

function Stop-WingenServiceProcess {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([int]$ProcessId)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $process) { return }
    if ($process.Name -ne 'wingen.audio.exe') {
        throw "Refusing to stop PID $ProcessId because it is $($process.Name), not wingen.audio.exe."
    }
    if (-not $PSCmdlet.ShouldProcess("PID $ProcessId", 'Stop wingen.audio service')) { return }
    Stop-Process -Id $ProcessId -Force -ErrorAction Stop
    $deadline = (Get-Date).AddSeconds(15)
    while (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        if ((Get-Date) -ge $deadline) { throw "wingen.audio PID $ProcessId did not release port $Port." }
        Start-Sleep -Milliseconds 250
    }
}

switch ($Action) {
    'Status' {
        Get-WingenAudioWorkerStatus -Engine $Engine -WorkerUrl "http://127.0.0.1:$Port" -TimeoutSeconds 10 |
            ConvertTo-Json -Depth 32 -Compress
        exit 0
    }
    'Stop' {
        if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
            $pidValue = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
            Stop-WingenServiceProcess -ProcessId $pidValue
            Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        }
        [pscustomobject]@{ ok = $true; engine = $Engine; port = $Port; stopped = $true } |
            ConvertTo-Json -Depth 8 -Compress
        exit 0
    }
    'Restart' {
        if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
            Stop-WingenServiceProcess -ProcessId ([int](Get-Content -LiteralPath $pidPath -Raw).Trim())
        }
    }
}

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($existing) {
    try {
        $status = Get-WingenAudioWorkerStatus -Engine $Engine -WorkerUrl "http://127.0.0.1:$Port" -TimeoutSeconds 5
        if ($status.controlMode -eq 'wingen.audio/mxc') {
            $existing.OwningProcess | Set-Content -LiteralPath $pidPath -Encoding ascii
            $status | ConvertTo-Json -Depth 32 -Compress
            exit 0
        }
    } catch {
        Write-Verbose "Port $Port has a non-Wingen listener: $($_.Exception.Message)"
    }
    throw "Port $Port is owned by PID $($existing.OwningProcess), not a wingen.audio service."
}

try {
    Get-WingenAudioWorkerStatus -Engine $Engine -WorkerUrl "http://127.0.0.1:$BackendPort" -TimeoutSeconds 5 |
        Out-Null
} catch {
    throw "$Engine model backend is unavailable on 127.0.0.1:$BackendPort. Start it before the native service."
}

$arguments = @(
    'worker-server', '--engine', $Engine,
    '--listen', "http://127.0.0.1:$Port",
    '--upstream', "http://127.0.0.1:$BackendPort",
    '--timeout', "$TimeoutSeconds"
)
$process = Start-Process -FilePath $native -ArgumentList $arguments `
    -WorkingDirectory $root -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
    -PassThru -WindowStyle Hidden
$process.Id | Set-Content -LiteralPath $pidPath -Encoding ascii
$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 250
    if ($process.HasExited) {
        throw "$Engine wingen.audio service exited $($process.ExitCode): $(Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue)"
    }
    try {
        $status = Get-WingenAudioWorkerStatus -Engine $Engine -WorkerUrl "http://127.0.0.1:$Port" -TimeoutSeconds 3
        if ($status.controlMode -eq 'wingen.audio/mxc') {
            $status | ConvertTo-Json -Depth 32 -Compress
            exit 0
        }
    } catch {
        Write-Verbose "$Engine native service is not ready yet: $($_.Exception.Message)"
    }
} while ((Get-Date) -lt $deadline)
Stop-WingenServiceProcess -ProcessId $process.Id
throw "$Engine wingen.audio service did not publish port $Port."
