#Requires -Version 7.0

. (Join-Path $PSScriptRoot 'execution.ps1')

function Assert-WingenLocalFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [long]$MaximumBytes = 16777216
    )
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -and $item.LinkType) {
        throw "Refusing symlinked model input: $Path"
    }
    if ($item.Length -le 0) { throw "Missing or empty local file: $Path" }
    if ($item.Length -gt $MaximumBytes) { throw "File exceeds $MaximumBytes bytes: $Path" }
    $item.FullName
}

function Get-WingenWavInfo {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [double]$MaximumSeconds = 30.0
    )
    $path = Assert-WingenLocalFile -Path $Path -MaximumBytes 29600844
    $stream = [IO.File]::OpenRead($path)
    try {
        $reader = [IO.BinaryReader]::new($stream)
        $riff = [Text.Encoding]::ASCII.GetString($reader.ReadBytes(4))
        [void]$reader.ReadUInt32()
        $wave = [Text.Encoding]::ASCII.GetString($reader.ReadBytes(4))
        $format = $null
        $dataBytes = 0
        while ($stream.Position + 8 -le $stream.Length) {
            $id = [Text.Encoding]::ASCII.GetString($reader.ReadBytes(4))
            $length = $reader.ReadUInt32()
            if ($id -eq 'fmt ' -and $length -ge 16) {
                $start = $stream.Position
                $format = [pscustomobject]@{
                    audio_format = $reader.ReadUInt16()
                    channels = $reader.ReadUInt16()
                    sample_rate = $reader.ReadUInt32()
                    byte_rate = $reader.ReadUInt32()
                    block_align = $reader.ReadUInt16()
                    bits = $reader.ReadUInt16()
                }
                $stream.Position = $start + $length
            } elseif ($id -eq 'data') {
                $dataBytes = $length
                $stream.Position += $length
            } else {
                $stream.Position += $length
            }
            if ($stream.Position % 2 -eq 1) { $stream.Position += 1 }
        }
        if ($riff -ne 'RIFF' -or $wave -ne 'WAVE' -or -not $format) {
            throw "Invalid WAV: $path"
        }
        if ($format.audio_format -ne 1 -or $format.channels -ne 1 -or $format.bits -ne 16) {
            throw "Expected mono PCM16 WAV: $path"
        }
        $samples = [int64]($dataBytes / 2)
        $duration = $samples / [double]$format.sample_rate
        if ($duration -le 0 -or $duration -gt $MaximumSeconds) {
            throw "WAV duration must be greater than 0 and at most $MaximumSeconds seconds: $path"
        }
        [pscustomobject]@{
            path = $path
            sample_rate = [int]$format.sample_rate
            samples = $samples
            duration_s = $duration
            channels = 1
            bits_per_sample = 16
        }
    } finally {
        $stream.Dispose()
    }
}

function Resolve-WingenVoicePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Raw,
        [Parameter(Mandatory = $true)][string]$VoiceRoot
    )
    $voiceRoot = [IO.Path]::GetFullPath($VoiceRoot)
    $candidate = if ([IO.Path]::IsPathFullyQualified($Raw)) {
        [IO.Path]::GetFullPath($Raw)
    } else {
        [IO.Path]::GetFullPath((Join-Path $voiceRoot $Raw))
    }
    $rootPrefix = $voiceRoot.TrimEnd('\', '/') + '\'
    if (-not ($candidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase))) {
        throw "Voice reference escapes GENAID voice root: $Raw"
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        $filename = [IO.Path]::GetFileName($Raw)
        $voiceMatches = @(Get-ChildItem -LiteralPath $voiceRoot -Recurse -File -Filter $filename)
        if ($voiceMatches.Count -ne 1) {
            throw "Voice reference is missing or ambiguous in GENAID library: $Raw"
        }
        $candidate = $voiceMatches[0].FullName
    }
    $candidate
}

function Invoke-WingenVibeVoicePreflight {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Request,
        [Parameter(Mandatory = $true)][string]$VoiceRoot,
        [string]$OutputRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'outputs'),
        [string]$PowerTorchRoot,
        [switch]$LoadOnly
    )
    $errors = [System.Collections.Generic.List[object]]::new()
    $report = [ordered]@{
        schema_version = '1.0'
        ok = $false
        ready = $false
        engine = 'vibevoice'
        capability = 'script_tts'
        model_dir = $Request.model_dir
        runtime = 'powertorch'
        assets = @{}
        errors = @()
    }
    try {
        [void](Import-WingenPowerTorch -Root $PowerTorchRoot)
        $root = [IO.Path]::GetFullPath($Request.model_dir)
        $configPath = Assert-WingenLocalFile -Path (Join-Path $root 'config.json')
        $processorPath = Assert-WingenLocalFile -Path (Join-Path $root 'preprocessor_config.json')
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json -Depth 32
        $processor = Get-Content -LiteralPath $processorPath -Raw | ConvertFrom-Json -Depth 32
        if ($config.model_type -ne 'vibevoice' -or $config.decoder_config.model_type -ne 'qwen2') {
            throw 'Unsupported VibeVoice model configuration.'
        }
        if ([int]$processor.speech_tok_compress_ratio -ne 3200 -or
            [int]$processor.audio_processor.sampling_rate -ne 24000) {
            throw 'Only 24 kHz / 3200-compress VibeVoice is supported.'
        }
        $indexPath = Join-Path $root 'model.safetensors.index.json'
        $checkpointNames = @()
        if (Test-Path -LiteralPath $indexPath -PathType Leaf) {
            $index = Get-Content -LiteralPath $indexPath -Raw | ConvertFrom-Json -AsHashtable
            $checkpointNames = @($index.weight_map.Values | Sort-Object -Unique)
            if ($checkpointNames.Count -eq 0) { throw 'VibeVoice shard index is empty.' }
        } else {
            $checkpointNames = @('model.safetensors')
        }
        $weights = @()
        foreach ($name in $checkpointNames) {
            if ([IO.Path]::IsPathFullyQualified($name) -or $name -match '(^|[/\\])\.\.([/\\]|$)') {
                throw "VibeVoice shard path is not local: $name"
            }
            $checkpoint = Assert-WingenLocalFile -Path (Join-Path $root $name) -MaximumBytes 107374182400
            $weights += Get-PowerTorchCheckpoint -Path $checkpoint
        }
        $tokenizer = Assert-WingenLocalFile -Path (Join-Path $Request.tokenizer 'tokenizer.json')
        foreach ($name in @('tokenizer_config.json', 'vocab.json', 'merges.txt')) {
            [void](Assert-WingenLocalFile -Path (Join-Path $Request.tokenizer $name))
        }
        $voices = @()
        $outputPath = $null
        if (-not $LoadOnly) {
            foreach ($reference in @($Request.voiceRefs)) {
                $voices += Get-WingenWavInfo -Path (Resolve-WingenVoicePath -Raw $reference -VoiceRoot $VoiceRoot)
            }
            if (-not $Request.output) { throw 'Generation requires an output WAV path.' }
            $resolvedOutputRoot = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\', '/')
            $outputPath = if ([IO.Path]::IsPathFullyQualified([string]$Request.output)) {
                [IO.Path]::GetFullPath([string]$Request.output)
            } else {
                [IO.Path]::GetFullPath((Join-Path $resolvedOutputRoot ([string]$Request.output)))
            }
            $outputPrefix = $resolvedOutputRoot + '\'
            if (-not $outputPath.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase) -or
                [IO.Path]::GetExtension($outputPath) -ne '.wav') {
                throw "Output must be a .wav beneath $resolvedOutputRoot."
            }
            if (Test-Path -LiteralPath $outputPath) {
                throw "Refusing to overwrite existing output: $outputPath"
            }
        }
        $backend = if ($Request.execution -and $Request.execution.backend) { $Request.execution.backend } else { 'Auto' }
        $precision = if ($Request.execution -and $Request.execution.precision) { $Request.execution.precision } else { 'auto' }
        $attention = if ($Request.execution -and $Request.execution.attention) { $Request.execution.attention } else { 'math' }
        $offload = if ($Request.execution -and $Request.execution.offload) { $Request.execution.offload } else { 'cpu' }
        $deviceIndex = if ($Request.execution -and $null -ne $Request.execution.device_index) { [int]$Request.execution.device_index } else { 0 }
        $gpuMemoryGiB = if ($Request.execution -and $null -ne $Request.execution.gpu_memory_gib) { [double]$Request.execution.gpu_memory_gib } else { 5.75 }
        $cpuMemoryGiB = if ($Request.execution -and $null -ne $Request.execution.cpu_memory_gib) { [double]$Request.execution.cpu_memory_gib } else { 48.0 }
        $execution = Get-WingenVibeVoiceExecution -Backend $backend -Precision $precision `
            -Attention $attention -Offload $offload -DeviceIndex $deviceIndex `
            -GpuMemoryGiB $gpuMemoryGiB -CpuMemoryGiB $cpuMemoryGiB -PowerTorchRoot $PowerTorchRoot `
            -ModelDir $Request.model_dir
        $os = Get-CimInstance Win32_OperatingSystem
        $availableRam = [int64]$os.FreePhysicalMemory * 1024
        $reserve = 2GB
        if ($availableRam -lt ($execution.cpu_memory_bytes + $reserve)) {
            $errors.Add((ConvertTo-WingenVibeVoiceError -Code 'insufficient_host_memory' `
                -Message ("PowerTorch hybrid load requires {0:N1} GiB CPU RAM plus a 2 GiB reserve; {1:N1} GiB is available." -f
                    ($execution.cpu_memory_bytes / 1GB), ($availableRam / 1GB))))
        }
        $report.assets = @{
            config = $config
            processor_config = $processor
            tokenizer = $tokenizer
            voices = $voices
            weights = $weights
            output = $outputPath
        }
        $report.memory = @{
            available_bytes = $availableRam
            cpu_budget_bytes = $execution.cpu_memory_bytes
            reserve_bytes = $reserve
        }
        $report.execution = $execution
        foreach ($missing in $execution.missing_capabilities) {
            $errors.Add((ConvertTo-WingenVibeVoiceError -Code 'unsupported_powertorch_capability' `
                -Message "PowerTorch lacks VibeVoice capability '$missing'." `
                -Details @{ capability = $missing }))
        }
    } catch {
        $errors.Add((ConvertTo-WingenVibeVoiceError -Code 'preflight_failed' -Message $_.Exception.Message))
    }
    $report.errors = @($errors)
    $report.ok = $errors.Count -eq 0
    $report.ready = $report.ok
    [pscustomobject]$report
}
