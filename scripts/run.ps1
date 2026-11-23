<#
.SYNOPSIS
Run a checkout-local Gen-Audio service with one model and runtime layout.
.DESCRIPTION
Models live in models/, logs and generated files in artifacts/runtime/.
Runs in the foreground. -Check prints the launch plan without starting anything.
Desktop is the native release app; Preview is the Vite development server.
#>
[CmdletBinding()]
param(
    [ValidateSet('Mcp', 'Node', 'Desktop', 'Preview')]
    [string]$Service = 'Mcp',
    [ValidateRange(1, 65535)]
    [int]$Port,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'lib\runtime-config.ps1')
$configArgs = @{ RepoRoot = $root }
if ($Port) {
    switch ($Service) {
        'Node' { $configArgs.NodePort = $Port }
        { $_ -in @('Mcp', 'Desktop') } { $configArgs.McpPort = $Port }
        'Preview' { $configArgs.PreviewPort = $Port }
    }
}
$config = Read-GenAudioRuntimeConfig @configArgs
$Port = [int]$config.ports[$Service]
$runtime = $config.runtime
$python = $config.python
$environment = Get-GenAudioRuntimeEnvironment $config
$environment.GEN_AUDIO_MCP_ADDR = "127.0.0.1:$($config.ports.Mcp)"
if ($Service -eq 'Node') { $environment.GEN_AUDIO_NODE_PORT = "$Port" }
if ($Service -eq 'Preview') { $environment.GEN_AUDIO_PREVIEW_PORT = "$Port" }
$cwd = $root
$commandArgs = @()
switch ($Service) {
    'Mcp' {
        $exe = $config.mcpExecutable
        $commandArgs = @('--http', "127.0.0.1:$Port")
    }
    'Node' {
        $exe = $python
        $commandArgs = @('-c', "from gen_audio.node_http import serve_loopback; serve_loopback(port=$Port).serve_forever()")
    }
    'Desktop' { $exe = $config.desktopExecutable }
    'Preview' {
        $exe = 'npm.cmd'
        $cwd = Join-Path $root 'apps\desktop'
        $commandArgs = @('run', 'dev', '--', '--host', '127.0.0.1', '--port', "$Port", '--strictPort')
    }
}
$logName = $Service.ToLowerInvariant()
$outLog = Join-Path $runtime "$logName.out.log"
$errLog = Join-Path $runtime "$logName.err.log"
if ($Check) {
    [pscustomobject]@{
        service = $Service; executable = $exe; arguments = $commandArgs
        workingDirectory = $cwd; environment = $environment
        stdout = $outLog; stderr = $errLog
    } | ConvertTo-Json -Depth 5
    return
}
# The desktop app owns the sidecar listener; every other service binds $Port.
# Fail before runtime or log setup with the owner named, rather than hiding a
# bare bind error deep in the service log.
if ($Service -ne 'Desktop') {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        $name = if ($owner) { $owner.ProcessName } else { 'unknown' }
        throw "Port $Port is already in use by PID $($listener.OwningProcess) ($name). Stop it or pass -Port <free port>."
    }
}
if ($Service -ne 'Preview') {
    foreach ($file in @($exe, $python)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            throw "Missing runtime dependency: $file. See README.md, Checkout-local deployment."
        }
    }
}
New-Item -ItemType Directory -Force -Path $runtime, $environment.GEN_AUDIO_WORK_DIR | Out-Null
$saved = @{}
foreach ($key in $environment.Keys) {
    $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
    Set-GenAudioProcessVariable -Name $key -Value $environment[$key]
}
Push-Location $cwd
try {
    Write-Output "Starting $Service on 127.0.0.1:$Port; logs: $runtime"
    & $exe @commandArgs 1>> $outLog 2>> $errLog
    if ($LASTEXITCODE -ne 0) { throw "$Service exited with code $LASTEXITCODE; see $errLog" }
} finally {
    Pop-Location
    foreach ($key in $saved.Keys) {
        Set-GenAudioProcessVariable -Name $key -Value $saved[$key]
    }
}
