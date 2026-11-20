#Requires -Version 5.1
<#
.SYNOPSIS
Bootstrap, configure, build, test, launch, and verify a Gen-Audio Windows node.

.DESCRIPTION
Run this entry point from a checkout or download it directly from GitHub. A Node
run preserves an existing checkout, installs the complete locked Python, Rust,
and npm framework, stages checksum-verified Kokoro assets, persists local
environment configuration, and launches the node, MCP, and UI preview services.

.EXAMPLE
pwsh -NoProfile -File .\scripts\toolbox.ps1 -Step Node -RepoRoot G:\gen-audio
.EXAMPLE
pwsh -NoProfile -File $env:TEMP\gen-audio-toolbox.ps1 -Step Node -RepoRoot G:\gen-audio
#>
[CmdletBinding()]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', '', Justification = 'Entry-point parameters are consumed by the nested implementation functions.')]
param(
    [ValidateSet('All', 'Node', 'Deps', 'Models', 'Build', 'Test', 'Start', 'Verify')]
    [string[]]$Step = @('All'),
    [string]$RepoRoot,
    [string]$CheckoutPath,
    [string]$RepoUrl = 'https://github.com/darbotlabs/gen-audio.git',
    [string]$RepoRef = 'main',
    [ValidatePattern('^24\.\d+\.\d+$')]
    [string]$NodeVersion = '24.14.1',
    [int]$NodePort = 8002,
    [int]$McpPort = 8765,
    [int]$PreviewPort = 1420,
    [string]$LogRoot = (Join-Path $env:LOCALAPPDATA 'gen-audio\toolbox\logs'),
    [switch]$SkipTests,
    [switch]$NoBrowser,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$PSNativeCommandUseErrorActionPreference = $false
if ($Help) {
    Get-Help $PSCommandPath -Detailed
    return
}

$script:RunStartedAt = Get-Date
$script:PathPrefix = @()
$script:OriginalPath = $env:Path
$script:LogPath = $null
$script:TranscriptPath = $null
$script:RuntimeDir = $null

function Resolve-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $paths = @($script:PathPrefix) + ($script:OriginalPath -split ';') + ($machine -split ';') + ($user -split ';')
    $env:Path = ($paths | Where-Object { $_ } | Select-Object -Unique) -join ';'
}

function Add-PathPrefix {
    param([Parameter(Mandatory = $true)][string]$Directory)
    if ((Test-Path -LiteralPath $Directory) -and $script:PathPrefix -notcontains $Directory) {
        $script:PathPrefix = @($Directory) + $script:PathPrefix
        Resolve-SessionPath
    }
}

function Get-Tool {
    param([Parameter(Mandatory = $true)][string[]]$Names)
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Source }
    }
    return $null
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    $stream = [IO.File]::Open((Resolve-Path -LiteralPath $LiteralPath).ProviderPath,
        [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash($stream)
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Write-ToolboxLog {
    param(
        [ValidateSet('INFO', 'WARN', 'ERROR', 'STEP', 'OK', 'CMD')]
        [string]$Level,
        [Parameter(Mandatory = $true)][string]$Message
    )
    $clean = ($Message -replace "`r?`n", ' ')
    $line = '{0} {1,-5} {2}' -f (Get-Date).ToString('o'), $Level, $clean
    if ($script:LogPath) {
        [IO.File]::AppendAllText($script:LogPath, $line + [Environment]::NewLine,
            (New-Object Text.UTF8Encoding($false)))
    }
    Write-Output $line
}

function Initialize-Logging {
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss-fff') + "-$PID"
    $script:LogPath = Join-Path $LogRoot "toolbox-$stamp.log"
    $script:TranscriptPath = Join-Path $LogRoot "toolbox-$stamp.transcript.log"
    try { Start-Transcript -LiteralPath $script:TranscriptPath -Force | Out-Null }
    catch { Write-ToolboxLog 'WARN' "Transcript not started: $($_.Exception.Message)" }
}

function Complete-Logging {
    try { Stop-Transcript | Out-Null } catch { Write-Output 'Transcript was not active.' }
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = $RepoRoot
    )
    if (-not (Test-Path -LiteralPath $WorkingDirectory)) {
        $WorkingDirectory = [IO.Directory]::GetCurrentDirectory()
    }
    Write-ToolboxLog 'CMD' "$FilePath $($Arguments -join ' ') (cwd: $WorkingDirectory)"
    $originalDirectory = [IO.Directory]::GetCurrentDirectory()
    Set-Location -LiteralPath $WorkingDirectory
    try {
        $global:LASTEXITCODE = 0
        & $FilePath @Arguments 2>&1 | ForEach-Object {
            $line = "$_"
            if ($line) { Write-ToolboxLog 'CMD' $line }
        }
        $code = $LASTEXITCODE
    } finally {
        Set-Location -LiteralPath $originalDirectory
    }
    if ($code -ne 0) {
        throw "$FilePath exited with ${code}: $($Arguments -join ' ')"
    }
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [string[]]$Extra = @(),
        [switch]$Machine
    )
    $winget = Get-Tool @('winget.exe')
    if (-not $winget) { throw 'WinGet is required to install system dependencies.' }
    $arguments = @(
        'install', '--id', $Id, '--exact', '--source', 'winget',
        '--silent', '--disable-interactivity',
        '--accept-source-agreements', '--accept-package-agreements'
    )
    if ($Machine) { $arguments += @('--scope', 'machine') }
    else { $arguments += @('--scope', 'user') }
    $arguments += $Extra
    try { Invoke-External $winget $arguments $RepoRoot }
    catch {
        if (-not (Get-Tool @($Id.Split('.')[-1]))) { throw }
        Write-ToolboxLog 'WARN' "$Id reports an existing installation."
    }
    Resolve-SessionPath
}

function Test-Checkout {
    $manifest = Join-Path $RepoRoot 'pyproject.toml'
    return (Test-Path -LiteralPath $manifest -PathType Leaf) -and
        (Select-String -LiteralPath $manifest -SimpleMatch 'name = "gen-audio"' -Quiet)
}

function Initialize-Repository {
    $candidate = $null
    if ($RepoRoot) { $candidate = $RepoRoot }
    elseif ($CheckoutPath) { $candidate = $CheckoutPath }
    else {
        $adjacent = Join-Path $PSScriptRoot '..'
        if ((Test-Path -LiteralPath (Join-Path $adjacent 'pyproject.toml') -PathType Leaf) -and
            (Select-String -LiteralPath (Join-Path $adjacent 'pyproject.toml') -SimpleMatch 'name = "gen-audio"' -Quiet)) {
            $candidate = $adjacent
        }
    }
    if (-not $candidate) {
        throw 'No gen-audio checkout was detected. Pass -RepoRoot, for example -RepoRoot G:\gen-audio.'
    }
    if (-not [IO.Path]::IsPathRooted($candidate)) {
        $candidate = Join-Path ([IO.Directory]::GetCurrentDirectory()) $candidate
    }
    $script:RepoRoot = [IO.Path]::GetFullPath($candidate)
    $RepoRoot = $script:RepoRoot
    if (Test-Checkout) {
        if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot '.git'))) {
            throw "A Gen-Audio tree exists at $RepoRoot but it is not a Git checkout."
        }
        Write-ToolboxLog 'OK' "Using existing checkout: $RepoRoot"
    } else {
        if ((Test-Path -LiteralPath $RepoRoot) -and
            @(Get-ChildItem -LiteralPath $RepoRoot -Force -ErrorAction SilentlyContinue).Count) {
            throw "Refusing to overwrite non-empty checkout path: $RepoRoot"
        }
        $git = Get-Tool @('git.exe')
        if (-not $git) { Install-WingetPackage -Id 'Git.Git'; $git = Get-Tool @('git.exe') }
        if (-not $git) { throw 'git was not found after installation.' }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RepoRoot) | Out-Null
        Invoke-External $git @('clone', '--branch', $RepoRef, '--single-branch', $RepoUrl, $RepoRoot) (Split-Path -Parent $RepoRoot)
        if (-not (Test-Checkout)) { throw "The cloned repository is not Gen-Audio: $RepoRoot" }
        Write-ToolboxLog 'OK' "Cloned $RepoUrl at $RepoRef to $RepoRoot"
    }
    $script:RuntimeDir = Join-Path $RepoRoot 'artifacts\runtime'
}

function Initialize-PackageNetwork {
    $python = Get-Tool @('python.exe')
    if ($python) {
        $indexLine = & $python -m pip config list 2>$null | Where-Object { $_ -match '^global\.index-url=' } | Select-Object -Last 1
        $index = if ($indexLine -match "^global\.index-url=(?:'([^']+)'|`"([^`"]+)`"|(\S+))$") {
            @($Matches[1], $Matches[2], $Matches[3] | Where-Object { $_ })[0]
        } else { $null }
        if ($index) {
            $env:PIP_INDEX_URL = "$index"
            $env:UV_DEFAULT_INDEX = "$index"
            $env:UV_INDEX_URL = "$index"
            Write-ToolboxLog 'INFO' "Python package index: $($env:UV_DEFAULT_INDEX)"
        }
    }
    $env:UV_SYSTEM_CERTS = 'true'
    $env:UV_HTTP_TIMEOUT = '300'
    $env:UV_RETRIES = '8'
}

function Install-Node {
    $node = Get-Tool @('node.exe')
    if ($node) {
        $actual = ((& $node --version) -replace '^v', '').Trim()
        if ($actual -match '^24\.\d+\.\d+$' -and [version]$actual -ge [version]$NodeVersion) {
            Write-ToolboxLog 'OK' "Node $actual -> $node"
            return $node
        }
    }
    $name = "node-v$NodeVersion-win-x64"
    $installDir = Join-Path $env:LOCALAPPDATA "gen-audio\toolbox\tools\$name"
    if (-not (Test-Path -LiteralPath (Join-Path $installDir 'node.exe') -PathType Leaf)) {
        $cache = Join-Path $env:TEMP ("gen-audio-node-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $cache | Out-Null
        try {
            $baseUrl = "https://nodejs.org/dist/v$NodeVersion"
            $checksums = (Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/SHASUMS256.txt" -TimeoutSec 60).Content
            $entry = [regex]::Match($checksums, "(?m)^([a-f0-9]{64}) +$([regex]::Escape("$name.zip"))\r?$")
            if (-not $entry.Success) { throw "Node checksum is missing for $name.zip." }
            $archive = Join-Path $cache "$name.zip"
            Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/$name.zip" -OutFile $archive -TimeoutSec 180
            if ((Get-FileSha256 $archive) -ne $entry.Groups[1].Value) {
                throw 'Node archive checksum mismatch.'
            }
            Expand-Archive -LiteralPath $archive -DestinationPath $cache
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $installDir) | Out-Null
            Move-Item -LiteralPath (Join-Path $cache $name) -Destination $installDir
        } finally {
            Remove-Item -LiteralPath $cache -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Add-PathPrefix $installDir
    $node = Join-Path $installDir 'node.exe'
    Write-ToolboxLog 'OK' "Node $NodeVersion -> $node"
    return $node
}

function Install-DependencySet {
    Write-ToolboxLog 'STEP' 'Installing system dependencies.'
    $git = Get-Tool @('git.exe')
    if (-not $git) { Install-WingetPackage -Id 'Git.Git' }
    $node = Install-Node
    if (-not (Get-Tool @('npm.cmd'))) { throw "npm was not found beside $node." }
    if (-not (Get-Tool @('uv.exe'))) { Install-WingetPackage -Id 'astral-sh.uv' }
    Invoke-External (Get-Tool @('uv.exe')) @('python', 'install', '3.14') $RepoRoot
    if (-not (Get-Tool @('ffmpeg.exe')) -or -not (Get-Tool @('ffprobe.exe'))) {
        Install-WingetPackage -Id 'Gyan.FFmpeg'
        Add-PathPrefix (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links')
    }
    if (-not (Get-Tool @('cargo.exe'))) {
        Install-WingetPackage -Id 'Rustlang.Rustup'
        Add-PathPrefix (Join-Path $env:USERPROFILE '.cargo\bin')
        Invoke-External (Get-Tool @('rustup.exe')) @('default', 'stable') $RepoRoot
    }
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $hasMsvc = $false
    if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
        $install = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        $hasMsvc = $LASTEXITCODE -eq 0 -and $install
    }
    if (-not $hasMsvc) {
        Install-WingetPackage -Id 'Microsoft.VisualStudio.2022.BuildTools' -Machine -Extra @(
            '--override', '--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'
        )
    }
    $edgeClients = @(
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients',
        'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients',
        'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients'
    )
    $webview = Get-ItemProperty -Path ($edgeClients | ForEach-Object { "$_\*" }) -ErrorAction SilentlyContinue |
        Where-Object { $_.PSObject.Properties['name'] -and $_.name -eq 'Microsoft Edge WebView2 Runtime' } |
        Select-Object -First 1
    if (-not $webview) { Install-WingetPackage -Id 'Microsoft.EdgeWebView2Runtime' -Machine }
    Initialize-PackageNetwork
    Write-ToolboxLog 'OK' "git=$(& git --version); node=$(& node --version); npm=$(& npm.cmd --version)"
    Write-ToolboxLog 'OK' "uv=$(& uv --version); cargo=$(& cargo --version); ffmpeg=$((& ffmpeg -version)[0])"
}

function Install-ModelSet {
    Write-ToolboxLog 'STEP' 'Staging checkout-local Kokoro model assets.'
    $models = Join-Path $RepoRoot 'models'
    New-Item -ItemType Directory -Force -Path $models | Out-Null
    $targets = @(
        @{
            Name = 'kokoro-v1.0.onnx'
            Uri = 'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx'
            Preferred = Join-Path $env:USERPROFILE 'Downloads\kokoro-onnx\kokoro-v1.0.onnx'
            PreferredHash = '38d9b475042451924cfc9313cdf1e996da27228ab51ffa96a69e7ac7c6942bfa'
            Hash = '7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5'
        },
        @{
            Name = 'voices-v1.0.bin'
            Uri = 'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin'
            Preferred = Join-Path $env:USERPROFILE 'Downloads\kokoro-onnx\voices-v1.0.bin'
            PreferredHash = 'bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d'
            Hash = 'bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d'
        }
    )
    foreach ($asset in $targets) {
        $destination = Join-Path $models $asset.Name
        $preferredValid = (Test-Path -LiteralPath $asset.Preferred -PathType Leaf) -and
            ((Get-FileSha256 $asset.Preferred) -eq $asset.PreferredHash)
        $currentValid = (Test-Path -LiteralPath $destination -PathType Leaf) -and
            ((Get-FileSha256 $destination) -in @($asset.Hash, $asset.PreferredHash))
        if ($preferredValid) {
            Copy-Item -LiteralPath $asset.Preferred -Destination $destination -Force
            Write-ToolboxLog 'OK' "Using normalized $($asset.Name) from the a2swe model registry."
        } elseif (-not $currentValid) {
            $download = Join-Path $env:TEMP ("gen-audio-" + [guid]::NewGuid().ToString('N') + "-" + $asset.Name)
            try {
                Write-ToolboxLog 'INFO' "Downloading $($asset.Name)"
                Invoke-WebRequest -UseBasicParsing -Uri $asset.Uri -OutFile $download -TimeoutSec 600
                if ((Get-FileSha256 $download) -ne $asset.Hash) {
                    throw "$($asset.Name) checksum mismatch."
                }
                Copy-Item -LiteralPath $download -Destination $destination -Force
            } finally {
                Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
            }
        }
        if ((Get-Item -LiteralPath $destination).Length -lt 1) {
            throw "$($asset.Name) is empty."
        }
        Write-ToolboxLog 'OK' "$($asset.Name) bytes=$((Get-Item $destination).Length) sha256=$(Get-FileSha256 $destination)"
    }
}

function Install-SpeechForkSet {
    # The DarbotLM forks are private, so they stay out of the public uv.lock.
    # They replace the locked PyPI kokoro-onnx in place (same version) and add
    # the kokoro and misaki runtimes that the kokoro_dayour engine imports.
    $pins = Get-Content -LiteralPath (Join-Path $RepoRoot 'speech-forks.json') -Raw | ConvertFrom-Json
    $requirements = @()
    foreach ($entry in $pins.PSObject.Properties) {
        if ($entry.Value -notmatch '^https://github\.com/(DarbotLM/(?:kokoro|kokoro-onnx|misaki))/commit/([a-f0-9]{40})$') {
            throw "Invalid speech fork reference for $($entry.Name): $($entry.Value)"
        }
        $extra = if ($entry.Name -eq 'misaki') { '[en]' } else { '' }
        $requirements += "$($entry.Name)$extra @ git+https://github.com/$($Matches[1]).git@$($Matches[2])"
    }
    Write-ToolboxLog 'STEP' 'Installing the pinned DarbotLM speech forks (kokoro, kokoro-onnx, misaki).'
    $python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    $arguments = @('-m', 'pip', 'install', '--disable-pip-version-check', '--force-reinstall', '--no-deps') + $requirements
    Invoke-External $python $arguments $RepoRoot
    $runtime = @('-m', 'pip', 'install', '--disable-pip-version-check') + ($requirements | Where-Object { $_ -notmatch '^kokoro-onnx ' })
    Invoke-External $python $runtime $RepoRoot
}
function Build-Framework {
    Install-DependencySet
    Write-ToolboxLog 'STEP' 'Installing and building the full Gen-Audio framework.'
    Initialize-PackageNetwork
    $uv = Get-Tool @('uv.exe')
    $venv = Join-Path $RepoRoot '.venv'
    $python = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Invoke-External $uv @('venv', '--python', '3.14', '--seed', $venv) $RepoRoot
    }
    & $python -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Invoke-External $python @('-m', 'ensurepip', '--upgrade') $RepoRoot
    }
    New-Item -ItemType Directory -Force -Path $script:RuntimeDir | Out-Null
    $requirements = Join-Path $script:RuntimeDir 'python-requirements.lock.txt'
    $torchRequirements = Join-Path $script:RuntimeDir 'torch-requirements.lock.txt'
    $fullRequirements = Join-Path $script:RuntimeDir 'python-requirements-full.lock.txt'
    Invoke-External $uv @(
        'export', '--frozen', '--all-extras',
        '--format', 'requirements-txt', '--no-emit-project',
        '--output-file', $fullRequirements
    ) $RepoRoot
    $requirementLines = [IO.File]::ReadAllLines($fullRequirements)
    $torchStart = [array]::IndexOf($requirementLines, @($requirementLines | Where-Object { $_ -match '^torch==' })[0])
    if ($torchStart -lt 0) { throw 'Frozen requirements did not contain torch.' }
    $torchEnd = $torchStart + 1
    while ($torchEnd -lt $requirementLines.Count -and $requirementLines[$torchEnd] -notmatch '^[A-Za-z0-9_.-]+==') {
        $torchEnd++
    }
    [IO.File]::WriteAllLines($torchRequirements, $requirementLines[$torchStart..($torchEnd - 1)], (New-Object Text.UTF8Encoding($false)))
    $mainLines = @()
    if ($torchStart -gt 0) { $mainLines += $requirementLines[0..($torchStart - 1)] }
    if ($torchEnd -lt $requirementLines.Count) { $mainLines += $requirementLines[$torchEnd..($requirementLines.Count - 1)] }
    [IO.File]::WriteAllLines($requirements, $mainLines, (New-Object Text.UTF8Encoding($false)))
    Invoke-External $python @(
        '-m', 'pip', 'install', '--disable-pip-version-check',
        '--only-binary=:all:', '--require-hashes', '--no-deps',
        '--trusted-host', 'download.pytorch.org',
        '--index-url', 'https://download.pytorch.org/whl/cpu',
        '-r', $torchRequirements
    ) $RepoRoot
    $pipArguments = @(
        '-m', 'pip', 'install', '--disable-pip-version-check',
        '--only-binary=:all:', '--require-hashes',
        '--trusted-host', 'files.pythonhosted.org',
        '--trusted-host', 'pypi.org',
        '-r', $requirements
    )
    if ($env:PIP_INDEX_URL) {
        $pipArguments += @('--index-url', $env:PIP_INDEX_URL)
    }
    Invoke-External $python $pipArguments $RepoRoot
    Invoke-External $python @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps', '-e', '.') $RepoRoot
    Invoke-External $uv @(
        'sync', '--frozen', '--all-extras', '--offline',
        '--no-install-project', '--inexact'
    ) $RepoRoot
    Install-SpeechForkSet
    Invoke-External $python @('-m', 'pip', 'check') $RepoRoot
    Invoke-External (Get-Tool @('npm.cmd')) @('ci') (Join-Path $RepoRoot 'apps\desktop')
    Invoke-External (Get-Tool @('npm.cmd')) @('run', 'build') (Join-Path $RepoRoot 'apps\desktop')
    Invoke-External (Get-Tool @('cargo.exe')) @(
        'build', '--locked', '--release',
        '-p', 'gen-audio-mcp', '-p', 'gen-audio-acp', '-p', 'gen-audio-harness'
    ) $RepoRoot
    Write-ToolboxLog 'OK' 'Python, npm, MCP, ACP, and harness builds completed.'
}

function Write-NodeConfiguration {
    $python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    $models = Join-Path $RepoRoot 'models'
    $configuration = [ordered]@{
        GEN_AUDIO_REPO = $RepoRoot
        GEN_AUDIO_PYTHON = $python
        GEN_AUDIO_MODEL_DIR = $models
        GEN_AUDIO_KOKORO_MODEL = Join-Path $models 'kokoro-v1.0.onnx'
        GEN_AUDIO_KOKORO_VOICES = Join-Path $models 'voices-v1.0.bin'
        GENAID_MODEL_BASE = $models
        GEN_AUDIO_MCP_ADDR = "127.0.0.1:$McpPort"
        GEN_AUDIO_WORK_DIR = Join-Path $script:RuntimeDir 'mcp'
    }
    New-Item -ItemType Directory -Force -Path $script:RuntimeDir | Out-Null
    foreach ($entry in $configuration.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, "$($entry.Value)", 'User')
        [Environment]::SetEnvironmentVariable($entry.Key, "$($entry.Value)", 'Process')
    }
    $legacyEnvironment = Join-Path $script:RuntimeDir 'node-environment.ps1'
    if (Test-Path -LiteralPath $legacyEnvironment) {
        Remove-Item -LiteralPath $legacyEnvironment -Force
    }
    $lines = @($configuration.GetEnumerator() | ForEach-Object {
        '$env:{0} = {1}' -f $_.Key, ([System.Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent("$($_.Value)") -replace '^', "'" -replace '$', "'")
    })
    [IO.File]::WriteAllLines(
        (Join-Path $script:RuntimeDir 'node-environment.txt'),
        $lines,
        (New-Object Text.UTF8Encoding($false))
    )
    Write-ToolboxLog 'OK' "Persisted Gen-Audio environment for $RepoRoot."
}

function Test-Url {
    param([Parameter(Mandatory = $true)][string]$Uri)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Invoke-NodeService {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Node', 'Mcp', 'Preview')][string]$Service,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$HealthUrl
    )
    $serviceName = $Service.ToLowerInvariant()
    $pidPath = Join-Path $script:RuntimeDir "$serviceName.pid"
    if (Test-Url $HealthUrl) {
        Write-ToolboxLog 'OK' "$Service is already healthy at $HealthUrl."
        return
    }
    if (Test-Path -LiteralPath $pidPath) {
        $recorded = [int](Get-Content -LiteralPath $pidPath -Raw)
        $existing = Get-Process -Id $recorded -ErrorAction SilentlyContinue
        if ($existing) {
            $record = Get-CimInstance Win32_Process -Filter "ProcessId=$recorded" -ErrorAction SilentlyContinue
            $expectedRun = Join-Path $RepoRoot 'scripts\run.ps1'
            if ($record -and $record.CommandLine -like "*$expectedRun*" -and $record.CommandLine -like "*$Service*") {
                Stop-Process -Id $existing.Id -Force
                Start-Sleep -Milliseconds 500
            } else {
                Write-ToolboxLog 'WARN' "PID $recorded was reused by another process; stale $serviceName PID file ignored."
            }
        }
        Remove-Item -LiteralPath $pidPath -Force
    }
    $powershell = Get-Tool @('pwsh.exe', 'powershell.exe')
    $run = Join-Path $RepoRoot 'scripts\run.ps1'
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $run, '-Service', $Service, '-Port', "$Port")
    $process = Start-Process -FilePath $powershell -ArgumentList $arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
    [IO.File]::WriteAllText($pidPath, "$($process.Id)", (New-Object Text.UTF8Encoding($false)))
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "$Service exited with $($process.ExitCode). See $script:RuntimeDir."
        }
        if (Test-Url $HealthUrl) {
            Write-ToolboxLog 'OK' "$Service healthy at $HealthUrl (launcher PID $($process.Id))."
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Service did not become healthy at $HealthUrl."
}

function Invoke-ServiceSuite {
    Write-ToolboxLog 'STEP' 'Launching Gen-Audio services.'
    Invoke-NodeService -Service Node -Port $NodePort -HealthUrl "http://127.0.0.1:$NodePort/genaid-audio/health"
    Invoke-NodeService -Service Mcp -Port $McpPort -HealthUrl "http://127.0.0.1:$McpPort/health"
    Invoke-NodeService -Service Preview -Port $PreviewPort -HealthUrl "http://127.0.0.1:$PreviewPort/"
    if (-not $NoBrowser) {
        Start-Process "http://127.0.0.1:$PreviewPort/"
    }
}

function Test-ServiceSuite {
    Write-ToolboxLog 'STEP' 'Verifying Gen-Audio services.'
    $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$NodePort/genaid-audio/health" -TimeoutSec 5 |
        Select-Object -ExpandProperty Content | ConvertFrom-Json
    if ($health.service -ne 'genaid-audio') { throw 'Node health response is not Gen-Audio.' }
    $ready = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$NodePort/genaid-audio/ready" -TimeoutSec 5 |
        Select-Object -ExpandProperty Content | ConvertFrom-Json
    if (-not $ready.ready) { throw 'Node readiness check failed.' }
    $mcpHealth = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$McpPort/health" -TimeoutSec 5 |
        Select-Object -ExpandProperty Content | ConvertFrom-Json
    if ($mcpHealth.service -ne 'gen-audio-mcp') { throw 'MCP health response is not Gen-Audio.' }
    $body = @{
        jsonrpc = '2.0'
        id = 1
        method = 'initialize'
        params = @{
            protocolVersion = '2025-03-26'
            capabilities = @{}
            clientInfo = @{ name = 'gen-audio-toolbox'; version = '1.0.0' }
        }
    } | ConvertTo-Json -Depth 8 -Compress
    $initialize = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$McpPort/mcp" `
        -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 5 |
        Select-Object -ExpandProperty Content | ConvertFrom-Json
    if ($initialize.result.serverInfo.name -ne 'gen-audio') { throw 'MCP initialize returned the wrong server.' }
    if (-not (Test-Url "http://127.0.0.1:$PreviewPort/")) { throw 'Preview is not reachable.' }
    Write-ToolboxLog 'OK' "Node=$NodePort MCP=$McpPort Preview=$PreviewPort; all health checks passed."
}

function Test-Framework {
    if ($SkipTests) {
        Write-ToolboxLog 'WARN' 'Framework validation skipped by -SkipTests.'
        return
    }
    Write-ToolboxLog 'STEP' 'Running the locked Gen-Audio validation gate.'
    $powershell = Get-Tool @('pwsh.exe', 'powershell.exe')
    $names = @(
        'GEN_AUDIO_REPO', 'GEN_AUDIO_PYTHON', 'GEN_AUDIO_MODEL_DIR',
        'GEN_AUDIO_KOKORO_MODEL', 'GEN_AUDIO_KOKORO_VOICES',
        'GENAID_MODEL_BASE', 'GEN_AUDIO_MCP_ADDR', 'GEN_AUDIO_MCP_ADDR_FILE',
        'GEN_AUDIO_WORK_DIR'
    )
    $saved = @{}
    foreach ($name in $names) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
    try {
        Invoke-External $powershell @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', (Join-Path $RepoRoot 'scripts\test.ps1'),
            '-Tag', 'node-bootstrap', '-FromLock',
            '-Python', (Join-Path $RepoRoot '.venv\Scripts\python.exe')
        ) $RepoRoot
    } finally {
        foreach ($name in $names) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
        }
    }
}

function Invoke-Toolbox {
    Initialize-Logging
    $exitCode = 0
    try {
        if ($env:OS -ne 'Windows_NT') { throw 'The Windows node toolbox requires Windows.' }
        Initialize-Repository
        $steps = if ($Step -contains 'All' -or $Step -contains 'Node') {
            @('Deps', 'Models', 'Build', 'Test', 'Start', 'Verify')
        } else {
            $Step
        }
        Write-ToolboxLog 'INFO' "RepoRoot=$RepoRoot Steps=$($steps -join ',') Log=$script:LogPath"
        Write-ToolboxLog 'INFO' "git HEAD $((git -C $RepoRoot rev-parse --short HEAD)); branch $((git -C $RepoRoot branch --show-current))"
        foreach ($current in $steps) {
            switch ($current) {
                'Deps' { Install-DependencySet }
                'Models' { Install-ModelSet }
                'Build' { Build-Framework; Write-NodeConfiguration }
                'Test' { Test-Framework }
                'Start' { Invoke-ServiceSuite }
                'Verify' { Test-ServiceSuite }
            }
        }
        Write-ToolboxLog 'OK' "Gen-Audio toolbox completed in $([math]::Round(((Get-Date) - $script:RunStartedAt).TotalMinutes, 1)) minutes."
    } catch {
        $exitCode = 1
        Write-ToolboxLog 'ERROR' "$($_.Exception.Message)"
        Write-ToolboxLog 'ERROR' "Bootstrap failed; inspect $script:LogPath and $script:RuntimeDir."
    } finally {
        Complete-Logging
        Write-Output "Log: $script:LogPath"
    }
    exit $exitCode
}

Invoke-Toolbox
