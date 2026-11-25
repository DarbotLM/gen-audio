#Requires -Version 7.0

. (Join-Path $PSScriptRoot 'contracts.ps1')

function Resolve-WingenPowerTorch {
    [CmdletBinding()]
    param([string]$Root)
    $pinPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'runtimes\powertorch.version.json'
    $pin = Get-Content -LiteralPath $pinPath -Raw | ConvertFrom-Json
    $candidates = @(
        $Root,
        $env:WINGEN_POWERTORCH_ROOT,
        'G:\PowerTorch',
        'D:\PowerTorch'
    ) | Where-Object { $_ }
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath (Join-Path $candidate '.git'))) { continue }
        $commit = git -C $candidate rev-parse HEAD 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
        $module = Join-Path $candidate $pin.module_relative_path
        [pscustomobject]@{
            root = $candidate
            commit = $commit.Trim()
            expected_commit = $pin.commit
            commit_matches = $commit.Trim() -eq $pin.commit
            module = $module
            module_exists = Test-Path -LiteralPath $module -PathType Leaf
        }
        return
    }
    throw 'PowerTorch checkout not found. Set WINGEN_POWERTORCH_ROOT or use G:\PowerTorch / D:\PowerTorch.'
}

function Import-WingenPowerTorch {
    [CmdletBinding()]
    param([string]$Root)
    $source = Resolve-WingenPowerTorch -Root $Root
    if (-not $source.commit_matches) {
        throw "PowerTorch commit is $($source.commit), expected $($source.expected_commit)."
    }
    if (-not $source.module_exists) {
        throw "PowerTorch module is missing: $($source.module). Run its Build-PowerTorch.ps1 script."
    }
    Import-Module $source.module -Force
    $source
}

function Get-WingenVibeVoiceExecution {
    [CmdletBinding()]
    param(
        [ValidateSet('Auto', 'Cpu', 'DirectML', 'Cuda')]
        [string]$Backend = 'Auto',
        [ValidateSet('auto', 'float32', 'float16', 'bfloat16')]
        [string]$Precision = 'auto',
        [ValidateSet('auto', 'math', 'efficient')]
        [string]$Attention = 'math',
        [ValidateSet('cpu', 'none')]
        [string]$Offload = 'cpu',
        [ValidateRange(0, 15)][int]$DeviceIndex = 0,
        [ValidateRange(0.5, 7.25)][double]$GpuMemoryGiB = 5.75,
        [ValidateRange(0.5, 60.0)][double]$CpuMemoryGiB = 48.0,
        [string]$PowerTorchRoot
    )
    $source = Import-WingenPowerTorch -Root $PowerTorchRoot
    $info = Get-PowerTorchInfo
    $devices = @{
        Cpu = Get-PowerTorchDevice -Backend Cpu
        DirectML = Get-PowerTorchDevice -Backend DirectML -AdapterIndex $DeviceIndex
        Cuda = Get-PowerTorchDevice -Backend Cuda -AdapterIndex $DeviceIndex
    }
    $required = @(
        'tensor.matmul', 'tensor.reshape', 'tensor.slice', 'tensor.transpose',
        'tensor.embedding', 'tensor.softmax', 'tensor.layer_norm', 'tensor.rms_norm',
        'tensor.rotary', 'tensor.causal_attention', 'tensor.conv1d',
        'model.safe_state_load', 'model.sharded_checkpoint', 'model.generation',
        'audio.codec', 'audio.diffusion', 'device.transfer', 'device.tensor_residency'
    )
    $selected = if ($Backend -eq 'Auto') {
        @('Cuda', 'DirectML', 'Cpu') | ForEach-Object {
            $name = $_
            $features = $devices[$name].Features
            $score = @($required | Where-Object { $features.ContainsKey($_) -and $features[$_] }).Count
            [pscustomobject]@{ name = $name; available = $devices[$name].IsAvailable; score = $score }
        } | Where-Object available | Sort-Object score -Descending | Select-Object -First 1 -ExpandProperty name
    } else { $Backend }
    $supported = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($feature in $devices[$selected].Features.GetEnumerator()) {
        if ($feature.Value) { [void]$supported.Add($feature.Key) }
    }
    $missing = @($required | Where-Object { -not $supported.Contains($_) })
    [pscustomobject]@{
        schema_version = '1.0'
        runtime = 'powertorch'
        source = $source
        version = $info.Version
        backend = $selected.ToLowerInvariant()
        device_index = $DeviceIndex
        precision = $Precision
        attention = $Attention
        offload = $Offload
        gpu_memory_bytes = [long]($GpuMemoryGiB * 1GB)
        cpu_memory_bytes = [long]($CpuMemoryGiB * 1GB)
        capability = $devices[$selected]
        ready = $missing.Count -eq 0
        missing_capabilities = $missing
    }
}

function Assert-WingenVibeVoiceExecution {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Execution)
    if (-not $Execution.ready) {
        throw "PowerTorch cannot run VibeVoice 7B yet; missing: $($Execution.missing_capabilities -join ', ')."
    }
    $Execution
}
