#Requires -Version 7.0
<#
.SYNOPSIS
Windows-native placement and control for Gen-Audio speech workers.

.DESCRIPTION
Extends the worker HTTP contract with PowerShell 7 Windows management: parallel
CIM/GPU/volume inventory, loopback worker health, SMB/output checks, SSH
transport, deterministic scoring, failover, and deployment receipts.
#>
[CmdletBinding()]
param(
    [ValidateSet('Status', 'Preflight', 'Load', 'Unload', 'Generate', 'Deploy', 'Benchmark')]
    [string]$Action = 'Status',
    [ValidateSet('vibevoice', 'personaplex')]
    [string]$Engine,
    [string]$Node,
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'runtimes\cluster.windows.json'),
    [string]$RequestPath,
    [string]$OutputPath,
    [switch]$NoFallback,
    [switch]$StageModels,
    [switch]$SkipRuntimeInstall,
    [ValidateRange(1, 7200)]
    [int]$TimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$clusterInvocation = @{
    Action = $Action
    Engine = $Engine
    RequestPath = $RequestPath
    OutputPath = $OutputPath
    TimeoutSeconds = $TimeoutSeconds
}
. (Join-Path $PSScriptRoot 'model_clients.ps1')
$Action = $clusterInvocation.Action
$Engine = $clusterInvocation.Engine
$RequestPath = $clusterInvocation.RequestPath
$OutputPath = $clusterInvocation.OutputPath
$TimeoutSeconds = $clusterInvocation.TimeoutSeconds

$script:WingenAudioClusterLimits = @{
    MaximumReceiptBytes = 4194304
    RequiredWorkerGiB = @{
        vibevoice = 26
        personaplex = 18
    }
    EnginePorts = @{
        vibevoice = 8996
        personaplex = 8997
    }
}

function Read-WingenAudioClusterConfig {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Cluster config not found: $Path"
    }
    $payload = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -Depth 32
    if ($payload.schema_version -ne '1.0') { throw 'Cluster config schema_version must be 1.0.' }
    if (-not $payload.nodes -or @($payload.nodes).Count -eq 0) {
        throw 'Cluster config needs at least one node.'
    }
    $nodes = @($payload.nodes)
    foreach ($node in $nodes) {
        if ($node.id -notmatch '^[a-z][a-z0-9_-]{0,31}$') {
            throw 'Cluster node id must be a lowercase slug.'
        }
        if ($node.ssh -notmatch '^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$') {
            throw "Cluster node $($node.id) ssh must be user@host."
        }
        foreach ($name in @('root', 'output_root', 'voice_root')) {
            if ([string]::IsNullOrWhiteSpace([string]$node.$name)) {
                throw "Cluster node $($node.id) needs $name."
            }
        }
        if (-not $node.model_dirs -or -not $node.model_dirs.vibevoice -or -not $node.model_dirs.personaplex) {
            throw "Cluster node $($node.id) must name both model_dirs."
        }
        if (-not $node.PSObject.Properties['weight']) { $node | Add-Member -NotePropertyName weight -NotePropertyValue 1.0 }
        $node.weight = [double]$node.weight
        if ($node.weight -le 0) { throw "Cluster node $($node.id) weight must be positive." }
    }
    $duplicates = @($nodes | Group-Object id | Where-Object Count -gt 1)
    if ($duplicates.Count) { throw "Cluster node ids must be unique: $($duplicates.Name -join ', ')." }
    return $nodes
}

function Get-WingenAudioNodeProbe {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Node)
    $quote = {
        param([string]$Value)
        return $Value.Replace("'", "''")
    }
    @"
`$ProgressPreference = 'SilentlyContinue'
`$ErrorActionPreference = 'SilentlyContinue'
`$os = Get-CimInstance Win32_OperatingSystem
`$cpu = @(Get-CimInstance Win32_Processor)
`$gpus = @()
if (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue) {
  foreach (`$line in @(& nvidia-smi.exe --query-gpu=index,name,memory.total,memory.free,memory.used,driver_version,utilization.gpu --format=csv,noheader,nounits 2>`$null)) {
    `$parts = `$line -split ',\s*'
    if (`$parts.Count -ge 7) {
      `$gpus += [pscustomobject]@{
        index=[int]`$parts[0]; name=`$parts[1]; total_mib=[int]`$parts[2]; free_mib=[int]`$parts[3]
        used_mib=[int]`$parts[4]; driver=`$parts[5]; utilization_percent=[int]`$parts[6]
      }
    }
  }
}
`$workers = @{}
foreach (`$entry in @([pscustomobject]@{name='vibevoice';port=8996},[pscustomobject]@{name='personaplex';port=8997})) {
  try {
    `$workers[`$entry.name] = Invoke-RestMethod -Uri "http://127.0.0.1:`$(`$entry.port)/v1/status" -TimeoutSec 3
  } catch {
    `$workers[`$entry.name] = [pscustomobject]@{ok=`$false;loaded=`$false;phase='offline'}
  }
}
`$outputWritable = `$false
`$probePath = Join-Path '$(& $quote ([string]$Node.output_root))' ('.wingen-audio-probe-' + [guid]::NewGuid().ToString('N'))
try {
  New-Item -ItemType Directory -Path '$(& $quote ([string]$Node.output_root))' -Force | Out-Null
  [IO.File]::WriteAllText(`$probePath, 'probe', [Text.Encoding]::ASCII)
  `$outputWritable = `$true
} catch { `$outputWritable = `$false }
finally { Remove-Item -LiteralPath `$probePath -Force -ErrorAction SilentlyContinue }
`$volumes = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
  [pscustomobject]@{device=`$_.DeviceID;size_bytes=[int64]`$_.Size;free_bytes=[int64]`$_.FreeSpace}
})
`$receipt = [pscustomobject]@{
  ok=`$true
  node_id='$(& $quote ([string]$Node.id))'
  host=`$env:COMPUTERNAME
  os=(Get-CimInstance Win32_OperatingSystem).Caption
  build=`$os.BuildNumber
  cpu_cores=(`$cpu | Measure-Object NumberOfLogicalProcessors -Sum).Sum
  cpu_groups=[Environment]::ProcessorCount
  ram_total_bytes=[int64]`$os.TotalVisibleMemorySize * 1024
  ram_free_bytes=[int64]`$os.FreePhysicalMemory * 1024
  gpu=`$gpus
  volumes=`$volumes
  root='$(& $quote ([string]$Node.root))'
  root_exists=Test-Path -LiteralPath '$(& $quote ([string]$Node.root))'
  output_root='$(& $quote ([string]$Node.output_root))'
  output_writable=`$outputWritable
  voice_root='$(& $quote ([string]$Node.voice_root))'
  voice_root_exists=Test-Path -LiteralPath '$(& $quote ([string]$Node.voice_root))'
  weight=$([double]$Node.weight)
  tools=[pscustomobject]@{
    powershell=(Get-Command pwsh.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    dotnet=(Get-Command dotnet.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    wingen=(Get-Command wingen.audio.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    git=(Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
  }
  model_paths=[pscustomobject]@{
    vibevoice='$(& $quote ([string]$Node.model_dirs.vibevoice))'
    vibevoice_exists=Test-Path -LiteralPath (Join-Path '$(& $quote ([string]$Node.model_dirs.vibevoice))' 'config.json')
    personaplex='$(& $quote ([string]$Node.model_dirs.personaplex))'
    personaplex_exists=Test-Path -LiteralPath (Join-Path '$(& $quote ([string]$Node.model_dirs.personaplex))' 'model.safetensors')
  }
  workers=`$workers
}
`$receipt | ConvertTo-Json -Compress -Depth 16
"@
}

function Invoke-WingenAudioRemoteJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Node,
        [Parameter(Mandatory = $true)][string]$Script,
        [ValidateRange(1, 7200)]
        [int]$TimeoutSeconds = 60
    )
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
    $local = [StringComparer]::OrdinalIgnoreCase.Equals([string]$Node.id, $env:COMPUTERNAME)
    $file = if ($local) { (Get-Process -Id $PID).Path } else { 'ssh' }
    $arguments = if ($local) {
        @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded)
    } else {
        @(
            '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o',
            "ConnectTimeout=$([Math]::Clamp([int]($TimeoutSeconds / 2), 5, 30))",
            [string]$Node.ssh, 'pwsh.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded
        )
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $file @arguments 2>&1 | ForEach-Object { "$_" })
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) {
        throw "$($Node.id) remote command failed ($exitCode): $(($output | Select-Object -Last 20) -join ' ')"
    }
    if ([Text.Encoding]::UTF8.GetByteCount($output -join "`n") -gt $script:WingenAudioClusterLimits.MaximumReceiptBytes) {
        throw "$($Node.id) returned an oversized cluster receipt."
    }
    $line = @($output | Where-Object { $_ -match '^\s*[\{\[]' }) | Select-Object -Last 1
    if (-not $line) { throw "$($Node.id) returned no JSON receipt." }
    return ConvertFrom-WingenAudioWorkerJson -Json $line
}

function Get-WingenAudioNodeStatus {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Node, [int]$TimeoutSeconds = 60)
    try {
        Invoke-WingenAudioRemoteJson -Node $Node -Script (Get-WingenAudioNodeProbe -Node $Node) -TimeoutSeconds $TimeoutSeconds
    } catch {
        [pscustomobject]@{
            ok = $false
            node_id = $Node.id
            errors = @([pscustomobject]@{ code = 'probe_failed'; message = $_.Exception.Message })
        }
    }
}

function Get-WingenAudioClusterReport {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]]$Nodes, [int]$TimeoutSeconds = 60)
    $receipts = foreach ($node in $Nodes) {
        Get-WingenAudioNodeStatus -Node $node -TimeoutSeconds $TimeoutSeconds
    }
    [pscustomobject]@{
        ok = $true
        schema_version = '1.0'
        generated_at = [DateTime]::UtcNow.ToString('O')
        nodes = @($receipts | Sort-Object node_id)
    }
}

function Get-WingenAudioNodeScore {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Report, [Parameter(Mandatory = $true)][string]$Engine)
    if (-not $Report.ok -or -not $Report.output_writable -or -not $Report.root_exists) {
        return [double]::NegativeInfinity
    }
    $modelReady = $Report.model_paths."$($Engine)_exists"
    if (-not $modelReady) { return [double]::NegativeInfinity }
    $required = [double]$script:WingenAudioClusterLimits.RequiredWorkerGiB[$Engine]
    $freeGiB = [double]$Report.ram_free_bytes / 1GB
    if ($freeGiB -lt $required) { return [double]::NegativeInfinity }

    $worker = $Report.workers.$Engine
    $score = 1.0
    if ($worker.loaded) { $score += 20.0 }
    elseif ($worker.phase -ne 'offline') { $score += 4.0 }
    if ($Report.gpu) {
        $free = @($Report.gpu | ForEach-Object { [double]$_.free_mib } | Sort-Object -Descending | Select-Object -First 1)
        $total = @($Report.gpu | ForEach-Object { [double]$_.total_mib } | Sort-Object -Descending | Select-Object -First 1)
        $score += [Math]::Min(20.0, [double]$total[0] / 1024.0)
        $score += [Math]::Min(12.0, [double]$free[0] / 1024.0)
        $utilization = @($Report.gpu | ForEach-Object { [double]$_.utilization_percent } | Measure-Object -Average).Average
        $score += [Math]::Max(0.0, (100.0 - $utilization) / 20.0)
    } else {
        $score += [double]$Report.cpu_cores / 8.0
    }
    $score += $freeGiB / 16.0
    if ($Report.tools.wingen) { $score += 3.0 }
    return $score * [double]$Report.weight
}

function Select-WingenAudioClusterNode {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Report,
        [Parameter(Mandatory = $true)][string]$Engine,
        [string]$Preferred
    )
    if ($Preferred) {
        $candidate = @($Report.nodes | Where-Object node_id -eq $Preferred)[0]
        if (-not $candidate) { throw "Unknown cluster node: $Preferred" }
        if ((Get-WingenAudioNodeScore -Report $candidate -Engine $Engine) -eq [double]::NegativeInfinity) {
            throw "Preferred node $Preferred cannot run $Engine."
        }
        return $candidate
    }
    $eligible = @(
        $Report.nodes | ForEach-Object {
            [pscustomobject]@{ Score = Get-WingenAudioNodeScore -Report $_ -Engine $Engine; Node = $_ }
        } | Where-Object Score -ne ([double]::NegativeInfinity) |
            Sort-Object @{ Expression = 'Score'; Descending = $true }, @{ Expression = { $_.Node.node_id }; Descending = $true }
    )
    if ($eligible.Count -eq 0) { throw "No cluster node is ready for $Engine." }
    return $eligible[0].Node
}

function Invoke-WingenAudioClusterAction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object[]]$Nodes,
        [Parameter(Mandatory = $true)][string]$Action,
        [string]$Engine,
        [string]$Preferred,
        [object]$Payload,
        [switch]$NoFallback,
        [int]$TimeoutSeconds = 1800
    )
    $report = Get-WingenAudioClusterReport -Nodes $Nodes -TimeoutSeconds ([Math]::Min(60, $TimeoutSeconds))
    if ($Action -eq 'Status' -or $Action -eq 'Benchmark') { return $report }
    $selected = Select-WingenAudioClusterNode -Report $report -Engine $Engine -Preferred $Preferred
    $ordered = @($selected)
    if (-not $NoFallback) {
        $ordered += @(
            $report.nodes | Where-Object node_id -ne $selected.node_id |
                Where-Object { (Get-WingenAudioNodeScore -Report $_ -Engine $Engine) -ne [double]::NegativeInfinity } |
                Sort-Object @{ Expression = { Get-WingenAudioNodeScore -Report $_ -Engine $Engine }; Descending = $true }
        )
    }
    $route = if ($Action -in @('Preflight', 'Load', 'Unload')) { $Action } else { 'Generate' }
    $failures = @()
    foreach ($candidate in $ordered) {
        $target = @($Nodes | Where-Object id -eq $candidate.node_id)[0]
        try {
            $body = ConvertTo-Json -InputObject $Payload -Depth 32 -Compress
            $port = $script:WingenAudioClusterLimits.EnginePorts[$Engine]
            $method = if ($route -eq 'Preflight') { 'Get' } else { 'Post' }
            $suffix = @{
                Preflight = '/v1/preflight'; Load = '/v1/load'; Unload = '/v1/unload'; Generate = '/v1/generate'
            }[$route]
            $requestBody = if ([string]::IsNullOrWhiteSpace($body)) { '{}' } else { $body }
            $encodedBody = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($requestBody))
            $remote = @"
`$body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encodedBody'))
try {
  if ('$method' -eq 'Get') {
    Invoke-RestMethod -Uri 'http://127.0.0.1:$port$suffix' -Method Get -TimeoutSec $TimeoutSeconds
  } else {
    Invoke-RestMethod -Uri 'http://127.0.0.1:$port$suffix' -Method Post -ContentType 'application/json' -Body `$body -TimeoutSec $TimeoutSeconds
  }
} catch {
  [pscustomobject]@{ok=`$false;errors=@([pscustomobject]@{code='worker_call_failed';message=`$_.Exception.Message})}
} | ConvertTo-Json -Compress -Depth 24
"@
            $receipt = Invoke-WingenAudioRemoteJson -Node $target -Script $remote -TimeoutSeconds ($TimeoutSeconds + 15)
            $receipt | Add-Member -NotePropertyName cluster -NotePropertyValue ([pscustomobject]@{
                selected_node = $candidate.node_id
                selected_score = Get-WingenAudioNodeScore -Report $candidate -Engine $Engine
                attempted_nodes = @($failures | ForEach-Object node_id) + $candidate.node_id
                failures = $failures
            }) -Force
            if ($Action -ne 'Generate' -or ($receipt.ok -eq $true -and $receipt.synthesizedSpeech -eq $true)) {
                return $receipt
            }
            $failures += @{ node_id = $candidate.node_id; receipt = $receipt }
        } catch {
            $failures += @{ node_id = $candidate.node_id; error = $_.Exception.Message }
        }
    }
    return [pscustomobject]@{
        ok = $false
        synthesizedSpeech = $false
        engine = $Engine
        cluster = @{ failures = $failures }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $nodes = @(Read-WingenAudioClusterConfig -Path $ConfigPath)
    if ($Node) { $nodes = @($nodes | Where-Object id -eq $Node) }
    if ($nodes.Count -eq 0) { throw "No cluster node matched '$Node'." }

    if ($Action -eq 'Deploy') {
        $deploy = Join-Path $PSScriptRoot 'deploy-cluster-node.ps1'
        foreach ($target in $nodes) {
            $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $deploy, '-Node', $target.id)
            if ($StageModels) { $arguments += '-StageModels' }
            if ($SkipRuntimeInstall) { $arguments += '-SkipRuntimeInstall' }
            & (Get-Process -Id $PID).Path @arguments
            if ($LASTEXITCODE -ne 0) { throw "$($target.id) deployment failed with exit $LASTEXITCODE." }
        }
        [pscustomobject]@{ ok = $true; action = 'deploy'; nodes = @($nodes.id) } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    if ($Action -notin @('Status', 'Benchmark') -and -not $Engine) {
        throw "$Action requires -Engine."
    }
    $payload = $null
    if ($Action -eq 'Generate') {
        if (-not $RequestPath -or -not (Test-Path -LiteralPath $RequestPath -PathType Leaf)) {
            throw 'Generate requires an existing -RequestPath.'
        }
        $payload = Get-Content -LiteralPath $RequestPath -Raw | ConvertFrom-Json -Depth 32
    }
    $receipt = Invoke-WingenAudioClusterAction -Nodes $nodes -Action $Action -Engine $Engine `
        -Preferred $Node -Payload $payload -NoFallback:$NoFallback -TimeoutSeconds $TimeoutSeconds
    if ($Action -eq 'Generate' -and $OutputPath -and $receipt.output) {
        if (-not (Test-Path -LiteralPath ([string]$receipt.output) -PathType Leaf)) {
            throw 'Cluster receipt names a missing generated output.'
        }
        Copy-Item -LiteralPath ([string]$receipt.output) -Destination $OutputPath -Force
        $receipt.output = [IO.Path]::GetFullPath($OutputPath)
    }
    $receipt | ConvertTo-Json -Depth 32 -Compress
    if ($receipt.ok -eq $false) { exit 2 }
}
