#Requires -Version 7.0
<#
.SYNOPSIS
Windows-native clients for isolated speech worker services.

.DESCRIPTION
Provides bounded, typed worker calls and a command-line surface for status,
preflight, load, unload, and generation. Dot-source this file to call the
WingenAudio functions from another PowerShell 7 control script.

The worker remains an implementation detail behind loopback HTTP. This script
owns Windows path validation, process-safe HTTP transport, response limits,
receipt checks, durable output promotion, and cleanup.
#>
[CmdletBinding()]
param(
    [ValidateSet('vibevoice', 'personaplex')]
    [string]$Engine,
    [ValidateSet('Status', 'Preflight', 'Load', 'Unload', 'Generate')]
    [string]$Action,
    [string]$WorkerUrl,
    [string]$RequestPath,
    [string]$OutputPath,
    [ValidateRange(1, 7200)]
    [int]$TimeoutSeconds = 1200
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:WingenAudioWorkerRoutes = @{
    vibevoice = @{
        EnvironmentName = 'GEN_AUDIO_VIBEVOICE_WORKER_URL'
        DefaultUrl = 'http://127.0.0.1:8996'
    }
    personaplex = @{
        EnvironmentName = 'GEN_AUDIO_PERSONAPLEX_WORKER_URL'
        DefaultUrl = 'http://127.0.0.1:8997'
    }
}

function Resolve-WingenAudioNativeHost {
    [CmdletBinding()]
    param([string]$ExplicitPath)
    $candidates = @(
        $ExplicitPath,
        $env:WINGEN_AUDIO_EXE,
        (Join-Path (Split-Path -Parent $PSScriptRoot) 'target\wingen.audio\publish\wingen.audio.exe'),
        (Join-Path (Split-Path -Parent $PSScriptRoot) 'src\wingen.audio\bin\Release\net11.0\win-x64\wingen.audio.exe'),
        (Get-Command wingen.audio.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    ) | Where-Object { $_ }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Invoke-WingenAudioNativeWorkerAction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$NativeHost,
        [Parameter(Mandatory = $true)][string]$Engine,
        [Parameter(Mandatory = $true)][string]$Action,
        [AllowNull()][object]$Payload,
        [string]$WorkerUrl,
        [int]$TimeoutSeconds,
        [string]$OutputPath
    )
    $arguments = @(
        'worker', '--action', $Action.ToLowerInvariant(), '--engine', $Engine,
        '--timeout', "$TimeoutSeconds"
    )
    if ($WorkerUrl) { $arguments += @('--url', $WorkerUrl) }
    if ($OutputPath) { $arguments += @('--output', $OutputPath) }
    $requestPath = $null
    if ($Action -eq 'Generate') {
        $requestPath = Join-Path ([IO.Path]::GetTempPath()) ('wingen-audio-request-' + [guid]::NewGuid().ToString('N') + '.json')
        $Payload | ConvertTo-Json -Depth 32 -Compress |
            Set-Content -LiteralPath $requestPath -Encoding UTF8
        $arguments += @('--request', $requestPath)
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $NativeHost @arguments 2>&1 | ForEach-Object { "$_" })
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
        if ($requestPath) { Remove-Item -LiteralPath $requestPath -Force -ErrorAction SilentlyContinue }
    }
    if ($exitCode -ne 0) {
        $detail = $output -join ' '
        if ($detail.Length -gt 4000) { $detail = $detail.Substring($detail.Length - 4000) }
        throw "wingen.audio.exe failed for $Engine $Action ($exitCode): $detail"
    }
    $json = @($output | Where-Object { $_ -match '^\s*\{' }) | Select-Object -Last 1
    if (-not $json) { throw 'wingen.audio.exe returned no worker receipt.' }
    return ConvertFrom-WingenAudioWorkerJson -Json $json
}

function Get-WingenAudioWorkerUrl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('vibevoice', 'personaplex')]
        [string]$Engine,
        [string]$WorkerUrl
    )
    if ($WorkerUrl) {
        $uri = $null
        if (-not [Uri]::TryCreate($WorkerUrl.TrimEnd('/'), [UriKind]::Absolute, [ref]$uri)) {
            throw "WorkerUrl for $Engine is not an absolute URI: $WorkerUrl"
        }
        if ($uri.Scheme -notin @('http', 'https')) {
            throw "WorkerUrl for $Engine must use HTTP or HTTPS."
        }
        return $uri.GetLeftPart([UriPartial]::Authority) + $uri.AbsolutePath.TrimEnd('/')
    }
    $definition = $script:WingenAudioWorkerRoutes[$Engine]
    $configured = [Environment]::GetEnvironmentVariable($definition.EnvironmentName, 'Process')
    if ($configured) { return (Get-WingenAudioWorkerUrl -Engine $Engine -WorkerUrl $configured) }
    return $definition.DefaultUrl
}

function ConvertFrom-WingenAudioWorkerJson {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Json, [int]$MaximumBytes = 4194304)
    if ([Text.Encoding]::UTF8.GetByteCount($Json) -gt $MaximumBytes) {
        throw "Worker response exceeds $MaximumBytes bytes."
    }
    try {
        $value = $Json | ConvertFrom-Json -Depth 32 -ErrorAction Stop
    } catch {
        throw "Worker returned invalid JSON: $($_.Exception.Message)"
    }
    if ($null -eq $value -or $value -isnot [pscustomobject]) {
        throw 'Worker returned a non-object response.'
    }
    return $value
}

function Invoke-WingenAudioWorkerRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('vibevoice', 'personaplex')]
        [string]$Engine,
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [AllowNull()]
        [object]$Payload,
        [string]$WorkerUrl,
        [ValidateRange(1, 7200)]
        [int]$TimeoutSeconds = 1200,
        [ValidateRange(0, 8)]
        [int]$RetryCount = 0
    )
    if (-not $Path.StartsWith('/')) { throw 'Worker path must start with "/".' }
    $base = Get-WingenAudioWorkerUrl -Engine $Engine -WorkerUrl $WorkerUrl
    $client = [Net.Http.HttpClient]::new()
    try {
        $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
        $client.DefaultRequestHeaders.Accept.ParseAdd('application/json')
        $attempt = 0
        while ($true) {
            $method = if ($null -eq $Payload) { [Net.Http.HttpMethod]::Get } else { [Net.Http.HttpMethod]::Post }
            $request = [Net.Http.HttpRequestMessage]::new($method, "$base$Path")
            try {
                if ($null -ne $Payload) {
                    $body = if ($Payload -is [string]) { $Payload } else { $Payload | ConvertTo-Json -Depth 32 -Compress }
                    $request.Content = [Net.Http.StringContent]::new($body, [Text.Encoding]::UTF8, 'application/json')
                }
                $response = $client.Send($request)
                try {
                    $stream = $response.Content.ReadAsStream()
                    try {
                        $buffer = [IO.MemoryStream]::new()
                        $chunk = [byte[]]::new(65536)
                        $total = 0
                        while (($read = $stream.Read($chunk, 0, $chunk.Length)) -gt 0) {
                            $total += $read
                            if ($total -gt 4194304) { throw 'Worker response exceeds 4194304 bytes.' }
                            $buffer.Write($chunk, 0, $read)
                        }
                        $json = [Text.Encoding]::UTF8.GetString($buffer.ToArray()).Trim()
                    } finally {
                        $buffer.Dispose()
                    }
                    $receipt = ConvertFrom-WingenAudioWorkerJson -Json $json
                    if ($response.IsSuccessStatusCode -or $null -ne $receipt.PSObject.Properties['errors']) {
                        return $receipt
                    }
                    throw "$Engine worker HTTP $([int]$response.StatusCode) returned an object without errors."
                } finally {
                    $response.Dispose()
                }
            } catch {
                if ($attempt -ge $RetryCount -or $_.Exception -is [OperationCanceledException]) { throw }
                $attempt += 1
                Start-Sleep -Milliseconds ([Math]::Min(2000, 100 * (1 -shl $attempt)))
            } finally {
                $request.Dispose()
            }
        }
    } finally {
        $client.Dispose()
    }
}

function Get-WingenAudioWorkerStatus {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Engine,
        [string]$WorkerUrl,
        [int]$TimeoutSeconds = 10
    )
    Invoke-WingenAudioWorkerRequest -Engine $Engine -Path '/v1/status' -WorkerUrl $WorkerUrl -TimeoutSeconds $TimeoutSeconds
}

function Assert-WingenAudioWorkerReady {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Engine,
        [string]$WorkerUrl,
        [int]$TimeoutSeconds = 10
    )
    $status = Get-WingenAudioWorkerStatus -Engine $Engine -WorkerUrl $WorkerUrl -TimeoutSeconds $TimeoutSeconds
    if ($status.ok -ne $true -or $status.loaded -ne $true) {
        $detail = if ($status.errors -and $status.errors.Count -gt 0) { $status.errors[0].message } else { $status.phase }
        if ([string]::IsNullOrWhiteSpace([string]$detail)) { $detail = 'model is unloaded' }
        throw "$Engine worker is not ready: $detail."
    }
    return $status
}

function Invoke-WingenAudioWorkerAction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Engine,
        [Parameter(Mandatory = $true)]
        [ValidateSet('Status', 'Preflight', 'Load', 'Unload', 'Generate')]
        [string]$Action,
        [string]$WorkerUrl,
        [AllowNull()][object]$Payload,
        [int]$TimeoutSeconds = 1200,
        [string]$OutputPath
    )
    $nativeHost = Resolve-WingenAudioNativeHost
    if ($nativeHost -and $env:WINGEN_AUDIO_FORCE_POWERSHELL -ne '1') {
        return Invoke-WingenAudioNativeWorkerAction -NativeHost $nativeHost -Engine $Engine -Action $Action `
            -Payload $Payload -WorkerUrl $WorkerUrl -TimeoutSeconds $TimeoutSeconds -OutputPath $OutputPath
    }
    $routes = @{
        Status = @{ Method = 'Get'; Path = '/v1/status'; Payload = $null }
        Preflight = @{ Method = 'Get'; Path = '/v1/preflight'; Payload = $null }
        Load = @{ Method = 'Post'; Path = '/v1/load'; Payload = @{} }
        Unload = @{ Method = 'Post'; Path = '/v1/unload'; Payload = @{} }
        Generate = @{ Method = 'Post'; Path = '/v1/generate'; Payload = $Payload }
    }
    $route = $routes[$Action]
    $receipt = Invoke-WingenAudioWorkerRequest -Engine $Engine -Path $route.Path -Payload $route.Payload `
        -WorkerUrl $WorkerUrl -TimeoutSeconds $TimeoutSeconds

    if ($Action -eq 'Load' -and ($receipt.ok -eq $false -or $receipt.loaded -ne $true)) {
        throw "$Engine worker failed to load: $((@($receipt.errors)[0]).message)"
    }
    if ($Action -eq 'Preflight' -and $receipt.ready -ne $true) {
        throw "$Engine worker preflight is not ready."
    }
    if ($Action -eq 'Generate') {
        if ($receipt.ok -ne $true -or $receipt.synthesizedSpeech -ne $true) {
            throw "$Engine worker failed to generate: $((@($receipt.errors)[0]).message)"
        }
        if ($OutputPath) {
            $source = [IO.Path]::GetFullPath([string]$receipt.output)
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
                throw "Worker returned a missing output path: $source"
            }
            $destination = [IO.Path]::GetFullPath($OutputPath)
            $parent = Split-Path -Parent $destination
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
            if (-not [StringComparer]::OrdinalIgnoreCase.Equals($source, $destination)) {
                $staging = "$destination.$([guid]::NewGuid().ToString('N')).stage"
                try {
                    Copy-Item -LiteralPath $source -Destination $staging -Force
                    Move-Item -LiteralPath $staging -Destination $destination -Force
                } finally {
                    Remove-Item -LiteralPath $staging -Force -ErrorAction SilentlyContinue
                }
            }
            $receipt.output = $destination
        }
    }
    return $receipt
}

if ($MyInvocation.InvocationName -ne '.') {
    if (-not $Engine -or -not $Action) {
        throw 'Direct invocation requires -Engine and -Action.'
    }
    $payload = $null
    if ($Action -eq 'Generate') {
        if (-not $RequestPath) { throw 'Generate requires -RequestPath.' }
        if (-not (Test-Path -LiteralPath $RequestPath -PathType Leaf)) {
            throw "Request file not found: $RequestPath"
        }
        $payload = ConvertFrom-WingenAudioWorkerJson -Json (Get-Content -LiteralPath $RequestPath -Raw)
    }
    Invoke-WingenAudioWorkerAction -Engine $Engine -Action $Action -WorkerUrl $WorkerUrl `
        -Payload $payload -TimeoutSeconds $TimeoutSeconds -OutputPath $OutputPath |
        ConvertTo-Json -Depth 32 -Compress
}
