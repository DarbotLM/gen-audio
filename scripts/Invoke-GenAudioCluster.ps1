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

$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'Cluster control requires PowerShell 7 or newer.'
}
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$nodes = @($config.nodes)
if ($Node) { $nodes = @($nodes | Where-Object id -eq $Node) }
if ($nodes.Count -eq 0) { throw "No cluster node matched '$Node'." }
if ($Action -ne 'Status' -and -not $Engine) {
    throw "$Action requires -Engine."
}

function Invoke-RemoteJson {
    param([object]$Target, [string]$Script)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
    if ($Target.id -eq $env:COMPUTERNAME.ToLowerInvariant()) {
        $output = & 'C:\Program Files\PowerShell\7\pwsh.exe' `
            -NoProfile -NonInteractive -EncodedCommand $encoded
        if ($LASTEXITCODE -ne 0) { throw "Local node probe failed for $($Target.id)." }
    } else {
        $output = & ssh -T -o BatchMode=yes -o IdentitiesOnly=yes $Target.ssh `
            pwsh.exe -NoProfile -NonInteractive -EncodedCommand $encoded
        if ($LASTEXITCODE -ne 0) { throw "SSH failed for $($Target.id)." }
    }
    $line = @($output | Where-Object { $_ -match '^\s*[\{\[]' })[-1]
    if (-not $line) { throw "Node $($Target.id) returned no JSON." }
    $line | ConvertFrom-Json
}

function Get-NodeStatus([object]$Target) {
    $probeTemplate = @'
$ErrorActionPreference = 'SilentlyContinue'
$workers = @{}
foreach ($entry in @(
  [pscustomobject]@{ name = 'vibevoice'; port = 8996 },
  [pscustomobject]@{ name = 'personaplex'; port = 8997 }
)) {
  try { $workers[$entry.name] = Invoke-RestMethod "http://127.0.0.1:$($entry.port)/v1/status" -TimeoutSec 3 }
  catch { $workers[$entry.name] = [pscustomobject]@{ loaded = $false; phase = 'offline' } }
}
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Measure-Object NumberOfLogicalProcessors -Sum
[pscustomobject]@{
  ok = $true
  node_id = '__NODE_ID__'
  ram_free_bytes = [int64]$os.FreePhysicalMemory * 1024
  cpu_cores = $cpu.Sum
  workers = $workers
  weight = [double]'__WEIGHT__'
} | ConvertTo-Json -Compress -Depth 8
'@
    $probe = $probeTemplate.
        Replace('__NODE_ID__', $Target.id.Replace("'", "''")).
        Replace('__WEIGHT__', [string]$Target.weight)
    Invoke-RemoteJson $Target $probe
}

$reports = foreach ($target in $nodes) {
    try { Get-NodeStatus $target }
    catch { [pscustomobject]@{ ok = $false; node_id = $target.id; error = $_.Exception.Message } }
}
if ($Action -eq 'Status') {
    [pscustomobject]@{ ok = $true; nodes = $reports } | ConvertTo-Json -Depth 16
    exit 0
}

function Get-Score([object]$Report) {
    if (-not $Report.ok) { return [double]::NegativeInfinity }
    $worker = $Report.workers.$Engine
    if ($worker.phase -eq 'offline') { return [double]::NegativeInfinity }
    $score = 1.0 + $(if ($worker.loaded) { 10.0 } else { 0.0 })
    $score += [double]$Report.cpu_cores / 12.0
    $score += ([double]$Report.ram_free_bytes / 1GB) / 16.0
    $score * [double]$Report.weight
}

$ordered = @($reports | Sort-Object { Get-Score $_ } -Descending)
$selected = if ($Node) { $ordered[0] } else { $ordered | Where-Object { (Get-Score $_) -ne [double]::NegativeInfinity } | Select-Object -First 1 }
if (-not $selected) { throw "No cluster node is ready for $Engine." }
$targets = if ($NoFallback) { @($selected) } else { @($selected) + @($ordered | Where-Object node_id -ne $selected.node_id) }

if ($Action -eq 'Generate') {
    if (-not $RequestPath -or -not (Test-Path -LiteralPath $RequestPath -PathType Leaf)) {
        throw 'Generate requires an existing -RequestPath.'
    }
    $body = Get-Content -LiteralPath $RequestPath -Raw
} else {
    $body = '{}'
}
$method = if ($Action -in @('Status', 'Preflight')) { 'Get' } else { 'Post' }
$suffix = @{
    Preflight = '/v1/preflight'
    Load = '/v1/load'
    Unload = '/v1/unload'
    Generate = '/v1/generate'
}[$Action]
$port = if ($Engine -eq 'vibevoice') { 8996 } else { 8997 }
$failures = @()
foreach ($report in $targets) {
    if (-not $report.ok) { continue }
    $target = $nodes | Where-Object id -eq $report.node_id | Select-Object -First 1
    $bodyBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($body))
    $script = @"
`$bytes = [Convert]::FromBase64String('$bodyBase64')
`$body = [Text.Encoding]::UTF8.GetString(`$bytes)
try {
  if ('$method' -eq 'Get') {
    Invoke-RestMethod "http://127.0.0.1:$port$suffix" -TimeoutSec $TimeoutSeconds
  } else {
    Invoke-RestMethod "http://127.0.0.1:$port$suffix" -Method Post -ContentType 'application/json' -Body `$body -TimeoutSec $TimeoutSeconds
  }
} catch {
  [pscustomobject]@{ ok=`$false; errors=@([pscustomobject]@{code='worker_call_failed';message=`$_.Exception.Message})}
} | ConvertTo-Json -Compress -Depth 24
"@
    $receipt = Invoke-RemoteJson $target $script
    if ($Action -ne 'Generate' -or ($receipt.ok -and $receipt.synthesizedSpeech)) {
        $receipt | Add-Member cluster @{ selected_node = $report.node_id; score = Get-Score $report; failures = $failures } -Force
        $receipt | ConvertTo-Json -Depth 32
        exit 0
    }
    $failures += @{ node_id = $report.node_id; receipt = $receipt }
}
[pscustomobject]@{ ok = $false; synthesizedSpeech = $false; engine = $Engine; cluster = @{ failures = $failures } } |
    ConvertTo-Json -Depth 32
exit 2

