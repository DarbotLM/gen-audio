Set-StrictMode -Version 2.0

function Resolve-GenAudioCheckoutRoot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $trimmed = $Path.Trim()
    if ($trimmed.Length -ge 2 -and
        (($trimmed[0] -eq '"' -and $trimmed[-1] -eq '"') -or
         ($trimmed[0] -eq "'" -and $trimmed[-1] -eq "'"))) {
        $trimmed = $trimmed.Substring(1, $trimmed.Length - 2).Trim()
    }
    if (-not $trimmed) { throw 'Checkout root is empty.' }
    $full = [IO.Path]::GetFullPath($trimmed)
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) {
        $full = $full.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    }
    if (Test-Path -LiteralPath $full) {
        $resolved = (Resolve-Path -LiteralPath $full -ErrorAction Stop).ProviderPath
        $full = if ($resolved.Length -gt [IO.Path]::GetPathRoot($resolved).Length) {
            $resolved.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
        } else { $resolved }
    }
    return $full
}

function Resolve-CheckoutPath {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$RelativePath)
    $normalizedRoot = Resolve-GenAudioCheckoutRoot $Root
    $path = (($RelativePath).Trim() -replace '/', [IO.Path]::DirectorySeparatorChar)
    return [IO.Path]::GetFullPath((Join-Path $normalizedRoot $path))
}

function Read-GenAudioRuntimeConfig {
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [int]$NodePort = 0,
        [int]$McpPort = 0,
        [int]$PreviewPort = 0
    )
    $root = Resolve-GenAudioCheckoutRoot $RepoRoot
    $defaultsPath = Join-Path $root 'scripts\runtime.json'
    if (-not (Test-Path -LiteralPath $defaultsPath -PathType Leaf)) {
        throw "Missing checkout runtime config: $defaultsPath"
    }
    $defaults = Get-Content -LiteralPath $defaultsPath -Raw | ConvertFrom-Json
    $runtimePath = Resolve-CheckoutPath $root $defaults.paths.runtime
    $descriptorPath = Join-Path $runtimePath 'node-environment.json'
    $descriptor = $null
    if (Test-Path -LiteralPath $descriptorPath -PathType Leaf) {
        try { $descriptor = Get-Content -LiteralPath $descriptorPath -Raw | ConvertFrom-Json }
        catch { throw "Invalid checkout runtime descriptor $descriptorPath`: $($_.Exception.Message)" }
        $descriptorRoot = Resolve-GenAudioCheckoutRoot "$($descriptor.checkout)"
        if (-not [StringComparer]::OrdinalIgnoreCase.Equals($descriptorRoot, $root)) {
            throw "Runtime descriptor belongs to $descriptorRoot, not $root"
        }
    }
    $ports = [ordered]@{
        Node = if ($NodePort -gt 0) { $NodePort } elseif ($descriptor -and $descriptor.ports.Node) { [int]$descriptor.ports.Node } else { [int]$defaults.ports.Node }
        Mcp = if ($McpPort -gt 0) { $McpPort } elseif ($descriptor -and $descriptor.ports.Mcp) { [int]$descriptor.ports.Mcp } else { [int]$defaults.ports.Mcp }
        Preview = if ($PreviewPort -gt 0) { $PreviewPort } elseif ($descriptor -and $descriptor.ports.Preview) { [int]$descriptor.ports.Preview } else { [int]$defaults.ports.Preview }
        Desktop = if ($McpPort -gt 0) { $McpPort } elseif ($descriptor -and $descriptor.ports.Desktop) { [int]$descriptor.ports.Desktop } else { [int]$defaults.ports.Desktop }
    }
    foreach ($value in $ports.Values) {
        if ($value -lt 1 -or $value -gt 65535) { throw "Runtime port $value is outside 1-65535" }
    }
    $runtime = Resolve-CheckoutPath $root $defaults.paths.runtime
    $models = Resolve-CheckoutPath $root $defaults.paths.models
    [pscustomobject]@{
        checkout = $root
        ports = $ports
        runtime = $runtime
        models = $models
        python = Resolve-CheckoutPath $root $defaults.paths.python
        mcpExecutable = Resolve-CheckoutPath $root $defaults.paths.mcpExecutable
        desktopExecutable = Resolve-CheckoutPath $root $defaults.paths.desktopExecutable
        mcpAddressFile = Resolve-CheckoutPath $root $defaults.paths.mcpAddress
        descriptor = $descriptorPath
    }
}

function Set-GenAudioProcessVariable {
    # PowerShell passes $null to a .NET string parameter as "", and on .NET 9 and
    # later [Environment]::SetEnvironmentVariable(name, "", 'Process') keeps an
    # empty variable instead of removing it. Rust's env::var then reads Some(""),
    # so unset means Remove-Item on the Env: drive.
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()][AllowEmptyString()][string]$Value
    )
    if (-not $PSCmdlet.ShouldProcess("Env:$Name", 'Set process environment variable')) { return }
    if ([string]::IsNullOrEmpty($Value)) {
        Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
    } else {
        Set-Item -LiteralPath "Env:$Name" -Value $Value
    }
}

function Get-GenAudioRuntimeEnvironment {
    param([Parameter(Mandatory = $true)]$Config)
    $onnxProvider = [Environment]::GetEnvironmentVariable('ONNX_PROVIDER', 'Process')
    if (-not $onnxProvider) { $onnxProvider = 'CPUExecutionProvider' }
    $kokoroDevice = [Environment]::GetEnvironmentVariable('GEN_AUDIO_KOKORO_DEVICE', 'Process')
    if (-not $kokoroDevice) { $kokoroDevice = 'cpu' }
    $allowHfDownload = [Environment]::GetEnvironmentVariable('GEN_AUDIO_ALLOW_HF_DOWNLOAD', 'Process')
    if (-not $allowHfDownload) { $allowHfDownload = '0' }
    $onnxTrim = [Environment]::GetEnvironmentVariable('GEN_AUDIO_KOKORO_TRIM', 'Process')
    if (-not $onnxTrim) { $onnxTrim = 'true' }
    $useMisaki = [Environment]::GetEnvironmentVariable('GEN_AUDIO_KOKORO_USE_MISAKI', 'Process')
    if (-not $useMisaki) { $useMisaki = 'false' }
    $kokoroModel = [Environment]::GetEnvironmentVariable('GEN_AUDIO_KOKORO_MODEL', 'Process')
    if (-not $kokoroModel) { $kokoroModel = Join-Path $Config.models 'kokoro-v1.0.onnx' }
    $kokoroVoices = [Environment]::GetEnvironmentVariable('GEN_AUDIO_KOKORO_VOICES', 'Process')
    if (-not $kokoroVoices) { $kokoroVoices = Join-Path $Config.models 'voices-v1.0.bin' }
    [ordered]@{
        GEN_AUDIO_REPO = $Config.checkout
        GEN_AUDIO_PYTHON = $Config.python
        GEN_AUDIO_MODEL_DIR = $Config.models
        GEN_AUDIO_KOKORO_MODEL = $kokoroModel
        GEN_AUDIO_KOKORO_VOICES = $kokoroVoices
        GEN_AUDIO_KOKORO_DEVICE = $kokoroDevice
        GEN_AUDIO_ALLOW_HF_DOWNLOAD = $allowHfDownload
        GEN_AUDIO_KOKORO_TRIM = $onnxTrim
        GEN_AUDIO_KOKORO_USE_MISAKI = $useMisaki
        ONNX_PROVIDER = $onnxProvider
        GENAID_MODEL_BASE = $Config.models
        GEN_AUDIO_WORK_DIR = Join-Path $Config.runtime 'mcp'
        GEN_AUDIO_MCP_ADDR = "127.0.0.1:$($Config.ports.Mcp)"
        GEN_AUDIO_MCP_ADDR_FILE = $Config.mcpAddressFile
        GEN_AUDIO_NODE_PORT = "$($Config.ports.Node)"
        GEN_AUDIO_PREVIEW_PORT = "$($Config.ports.Preview)"
    }
}
