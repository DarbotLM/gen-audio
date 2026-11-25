#Requires -Version 7.0
<#
.SYNOPSIS
Windows-native Gen-Audio plugin and remote-execution host.

.DESCRIPTION
Replaces the Python adapter discovery boundary with explicit PowerShell plugin
contracts. Native VibeVoice and PersonaPlex operations are dispatched through
wingen.audio.exe. Remote execution supports Windows OpenSSH, a logged-in WSL
distribution's SSH configuration, and PowerShell Remoting.
#>
[CmdletBinding()]
param(
    [ValidateSet('List', 'Probe', 'Generate', 'RemoteProbe', 'RemotingStatus', 'EnableRemoting')]
    [string]$Action = 'List',
    [ValidateSet('vibevoice', 'personaplex')]
    [string]$Engine,
    [string]$RequestPath,
    [string]$OutputPath,
    [string]$WorkerUrl,
    [string]$Node,
    [ValidateSet('Auto', 'WindowsSsh', 'WslSsh', 'PowerShell')]
    [string]$Transport = 'Auto',
    [int]$TimeoutSeconds = 1200,
    [string]$Script
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$pluginInvocation = @{
    Action = $Action
    Engine = $Engine
    RequestPath = $RequestPath
    OutputPath = $OutputPath
    WorkerUrl = $WorkerUrl
    Node = $Node
    Transport = $Transport
    TimeoutSeconds = $TimeoutSeconds
    Script = $Script
}
. (Join-Path $PSScriptRoot 'model_clients.ps1')
$Action = $pluginInvocation.Action
$Engine = $pluginInvocation.Engine
$RequestPath = $pluginInvocation.RequestPath
$OutputPath = $pluginInvocation.OutputPath
$WorkerUrl = $pluginInvocation.WorkerUrl
$Node = $pluginInvocation.Node
$Transport = $pluginInvocation.Transport
$TimeoutSeconds = $pluginInvocation.TimeoutSeconds
$Script = $pluginInvocation.Script

function Get-WingenPluginRegistry {
    param([string]$Engine)
    $registry =
    [ordered]@{
        vibevoice = [pscustomobject]@{
            capability = 'script_tts'
            action_script = Join-Path $PSScriptRoot 'vibevoice.ps1'
            native_engine = 'vibevoice'
            routes = @('status', 'preflight', 'load', 'unload', 'generate')
        }
        personaplex = [pscustomobject]@{
            capability = 'speech_to_speech'
            action_script = Join-Path $PSScriptRoot 'service.ps1'
            native_engine = 'personaplex'
            routes = @('status', 'preflight', 'load', 'unload', 'generate')
        }
    }
    if ($Engine) { return $registry[$Engine] }
    $registry
}

function Get-WingenSshContext {
    [CmdletBinding()]
    param([ValidateSet('Auto', 'WindowsSsh', 'WslSsh')][string]$Store = 'Auto')
    $windowsSsh = Get-Command ssh.exe -ErrorAction SilentlyContinue
    $windowsStore = Join-Path $HOME '.ssh'
    $wslDistro = $null
    $wslUser = $null
    $wslHome = $null
    if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $probe = @(& wsl.exe -e sh -lc 'printf "%s|%s\n" "$USER" "$HOME"; test -d "$HOME/.ssh"' 2>$null)
            if ($LASTEXITCODE -eq 0 -and $probe.Count -gt 0) {
                $parts = ([string]$probe[0]).Trim() -split '\|', 2
                if ($parts.Count -eq 2) {
                    $wslUser = $parts[0]
                    $wslHome = $parts[1]
                    $wslDistro = @(& wsl.exe -l -q 2>$null)[0]
                }
            }
        } finally {
            $ErrorActionPreference = $previous
        }
    }
    $selected = $Store
    if ($selected -eq 'Auto') {
        $selected = if ($wslUser -and $wslHome) { 'WslSsh' } else { 'WindowsSsh' }
    }
    if ($selected -eq 'WslSsh' -and -not $wslUser) {
        throw 'WSL SSH was requested, but no WSL distribution with a logged-in SSH store is available.'
    }
    if ($selected -eq 'WindowsSsh' -and (-not $windowsSsh -or -not (Test-Path -LiteralPath $windowsStore))) {
        throw "Windows SSH is unavailable or $windowsStore is missing."
    }
    [pscustomobject]@{
        selected = $selected
        windows_ssh = $windowsSsh.Source
        windows_home = $HOME
        windows_store = $windowsStore
        wsl_distribution = $wslDistro
        wsl_user = $wslUser
        wsl_home = $wslHome
        wsl_store = if ($wslHome) { "$wslHome/.ssh" } else { $null }
    }
}

function Get-WingenRemotingStatus {
    [CmdletBinding()]
    param()
    try {
        $configuration = @(Get-PSSessionConfiguration -ErrorAction Stop)
        $configurationDiagnostic = $null
    } catch {
        $configuration = @()
        $configurationDiagnostic = $_.Exception.Message
    }
    $service = Get-Service WinRM -ErrorAction SilentlyContinue
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).
        IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    [pscustomobject]@{
        enabled = $configuration.Count -gt 0 -and $service -and $service.Status -eq 'Running'
        service = if ($service) { [string]$service.Status } else { 'missing' }
        endpoints = @($configuration | ForEach-Object Name)
        elevated = $admin
        elevation_command = 'Start-Process pwsh -Verb RunAs -ArgumentList "-NoProfile -File scripts\plugins.ps1 -Action EnableRemoting"'
        configuration_diagnostic = $configurationDiagnostic
    }
}

function Enable-WingenPowerShellRemoting {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param()
    $status = Get-WingenRemotingStatus
    if ($status.enabled) { return $status }
    if (-not $status.elevated) {
        throw "PowerShell Remoting requires elevation. Run: $($status.elevation_command)"
    }
    if (-not $PSCmdlet.ShouldProcess($env:COMPUTERNAME, 'Enable PowerShell Remoting')) { return $status }
    Enable-PSRemoting -Force -SkipNetworkProfileCheck -ErrorAction Stop
    Get-WingenRemotingStatus
}

function Invoke-WingenRemoteCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$Script,
        [ValidateSet('Auto', 'WindowsSsh', 'WslSsh', 'PowerShell')]
        [string]$Transport = 'Auto',
        [int]$TimeoutSeconds = 60
    )
    if ($Transport -eq 'Auto') {
        $remoting = Get-WingenRemotingStatus
        $Transport = if ($remoting.enabled) { 'PowerShell' } else { 'WindowsSsh' }
    }
    if ($Transport -eq 'PowerShell') {
        $computerName = ($Target -split '@')[-1]
        $job = Invoke-Command -ComputerName $computerName -ScriptBlock ([scriptblock]::Create($Script)) -AsJob
        Wait-Job $job -Timeout $TimeoutSeconds | Out-Null
        if ($job.State -ne 'Completed') {
            Stop-Job $job
            throw "PowerShell Remoting to $Target timed out."
        }
        return Receive-Job $job
    }
    $context = Get-WingenSshContext -Store $Transport
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
    $arguments = @(
        '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
        '-o', "ConnectTimeout=$([Math]::Min(30, [Math]::Max(5, $TimeoutSeconds)))",
        $Target, 'pwsh.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded
    )
    $ssh = if ($Transport -eq 'WslSsh') { 'wsl.exe' } else { $context.windows_ssh }
    $sshArguments = if ($Transport -eq 'WslSsh') { @('-e', 'ssh') + $arguments } else { $arguments }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $ssh @sshArguments 2>&1 | ForEach-Object { "$_" })
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) {
        throw "SSH to $Target through $($context.selected) failed ($exitCode): $(($output | Select-Object -Last 20) -join ' ')"
    }
    $output
}

function Invoke-WingenPlugin {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidateSet('vibevoice', 'personaplex')][string]$Engine,
        [ValidateSet('Status', 'Preflight', 'Load', 'Unload', 'Generate')]
        [string]$PluginAction,
        [string]$RequestPath,
        [string]$OutputPath,
        [string]$WorkerUrl,
        [string]$Node,
        [ValidateSet('Auto', 'WindowsSsh', 'WslSsh', 'PowerShell')][string]$Transport = 'Auto',
        [int]$TimeoutSeconds = 1200
    )
    $plugin = Get-WingenPluginRegistry $Engine
    if (-not $plugin) { throw "No native PowerShell plugin is registered for $Engine." }
    if ($Node) {
        $port = if ($Engine -eq 'vibevoice') { 8996 } else { 8997 }
        $base = if ($WorkerUrl) { $WorkerUrl.TrimEnd('/') } else { "http://127.0.0.1:$port" }
        $route = @{
            Status = '/v1/status'; Preflight = '/v1/preflight'; Load = '/v1/load'
            Unload = '/v1/unload'; Generate = '/v1/generate'
        }[$PluginAction]
        $method = if ($PluginAction -in @('Status', 'Preflight')) { 'Get' } else { 'Post' }
        $body = if ($PluginAction -eq 'Generate') {
            if (-not $RequestPath) { throw 'Remote Generate requires -RequestPath.' }
            [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(
                (Get-Content -LiteralPath $RequestPath -Raw)))
        } else {
            [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('{}'))
        }
        $remote = @"
`$body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$body'))
`$parameters = @{ Uri = '$base$route'; Method = '$method'; TimeoutSec = $TimeoutSeconds }
if ('$method' -eq 'Post') {
  `$parameters.ContentType = 'application/json'
  `$parameters.Body = `$body
}
Invoke-RestMethod @parameters | ConvertTo-Json -Compress -Depth 32
"@
        return Invoke-WingenRemoteCommand -Target $Node -Script $remote `
            -Transport $Transport -TimeoutSeconds $TimeoutSeconds
    }
    switch ($PluginAction) {
        'Preflight' {
            if ($Engine -ne 'vibevoice') { throw 'PersonaPlex native preflight orchestration is not implemented yet.' }
            return & $plugin.action_script -Action Preflight -RequestPath $RequestPath
        }
        'Load' {
            if ($Engine -eq 'vibevoice') { return & $plugin.action_script -Action Load -RequestPath $RequestPath }
            return & $plugin.action_script -Engine personaplex -Action Start -TimeoutSeconds $TimeoutSeconds
        }
        'Unload' {
            if ($Engine -eq 'vibevoice') { return & $plugin.action_script -Action Unload }
            return & $plugin.action_script -Engine personaplex -Action Stop
        }
        'Generate' {
            Invoke-WingenAudioWorkerAction -Engine $Engine -Action Generate `
                -Payload (Get-Content -LiteralPath $RequestPath -Raw | ConvertFrom-Json) `
                -WorkerUrl $WorkerUrl -TimeoutSeconds $TimeoutSeconds -OutputPath $OutputPath
        }
        default {
            Invoke-WingenAudioWorkerAction -Engine $Engine -Action Status -WorkerUrl $WorkerUrl `
                -TimeoutSeconds ([Math]::Min(30, $TimeoutSeconds))
        }
    }
}

switch ($Action) {
    'List' { Get-WingenPluginRegistry | ConvertTo-Json -Depth 8 }
    'RemotingStatus' { Get-WingenRemotingStatus | ConvertTo-Json -Depth 8 -Compress }
    'EnableRemoting' { Enable-WingenPowerShellRemoting | ConvertTo-Json -Depth 8 -Compress }
    'RemoteProbe' {
        if (-not $Node) { throw 'RemoteProbe requires -Node.' }
        $context = Get-WingenSshContext -Store $(if ($Transport -in @('Auto', 'PowerShell')) { 'Auto' } else { $Transport })
        if (-not $Script) {
            $Script = '$PSVersionTable.PSVersion.ToString()'
        }
        [pscustomobject]@{
            ssh = $context
            result = @(Invoke-WingenRemoteCommand -Target $Node -Script $Script `
                -Transport $Transport -TimeoutSeconds $TimeoutSeconds)
        } | ConvertTo-Json -Depth 16
    }
    'Probe' {
        if (-not $Engine) { throw 'Probe requires -Engine.' }
        Invoke-WingenPlugin -Engine $Engine -PluginAction Status -WorkerUrl $WorkerUrl `
            -TimeoutSeconds $TimeoutSeconds | ConvertTo-Json -Depth 32 -Compress
    }
    'Generate' {
        if (-not $Engine -or -not $RequestPath) { throw 'Generate requires -Engine and -RequestPath.' }
        Invoke-WingenPlugin -Engine $Engine -PluginAction Generate -RequestPath $RequestPath `
            -OutputPath $OutputPath -WorkerUrl $WorkerUrl -Node $Node `
            -Transport $Transport -TimeoutSeconds $TimeoutSeconds | ConvertTo-Json -Depth 32 -Compress
    }
}
