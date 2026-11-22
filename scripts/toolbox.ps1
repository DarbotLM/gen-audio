#Requires -Version 5.1
<#
.SYNOPSIS
Bootstrap, configure, build, test, launch, and verify a Gen-Audio Windows node.

.DESCRIPTION
Run this entry point from a checkout or download it directly from GitHub. A Node
run preserves an existing checkout, installs the complete locked Python, Rust,
and npm framework, stages checksum-verified Kokoro assets, persists local
checkout-scoped runtime configuration, and launches the node, MCP, and UI
preview services. Overlapping runs of the same checkout are rejected by a named
lease. Skipped validation or best-effort warnings produce a degraded exit code.

.EXAMPLE
pwsh -NoProfile -File .\scripts\toolbox.ps1 -Step Node -RepoRoot G:\gen-audio
.EXAMPLE
pwsh -NoProfile -File $env:TEMP\gen-audio-toolbox.ps1 -Step Node -RepoRoot G:\gen-audio
.PARAMETER AcceptPackageAgreement
WinGet package ids whose package license the operator has reviewed and accepts
for this run, for example 'Rustlang.Rustup'. A missing dependency whose id is
not listed stops the run with the exact install command instead of accepting
its agreement automatically.
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
    [string]$NodeVersion = '26.13.1',
    [ValidateRange(0, 65535)][int]$NodePort = 0,
    [ValidateRange(0, 65535)][int]$McpPort = 0,
    [ValidateRange(0, 65535)][int]$PreviewPort = 0,
    [string]$LogRoot = (Join-Path $env:LOCALAPPDATA 'gen-audio\toolbox\logs'),
    [string[]]$AcceptPackageAgreement = @(),
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$UvMinimumVersion = '0.9.0',
    [ValidatePattern('^\d+\.\d+(\.\d+)?$')]
    [string]$PythonVersion = '3.14',
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
$script:RuntimeConfig = $null
$script:LeaseMutex = $null
$script:LeaseDigest = ''
$script:LeaseOwned = $false
$script:BestEffortWarnings = New-Object System.Collections.Generic.List[string]
$script:ValidationSkipped = $false
# APPINSTALLER_CLI_ERROR_NO_APPLICABLE_INSTALLER (0x8A150010)
$script:WingetNoApplicableInstaller = -1978335216
# ERROR_INSTALL_USEREXIT (1602) and ERROR_CANCELLED (1223): a declined, timed-out
# or unattended UAC prompt for a machine-scope installer.
$script:InstallerCancelledCodes = @(1602, 1223)

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

function Resolve-ToolboxCheckoutRoot {
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

function Resolve-ToolboxRepositoryTarget {
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
    if (-not [IO.Path]::IsPathRooted($candidate.Trim())) {
        $candidate = Join-Path ([IO.Directory]::GetCurrentDirectory()) $candidate.Trim()
    }
    $script:RepoRoot = Resolve-ToolboxCheckoutRoot $candidate
    $RepoRoot = $script:RepoRoot
}

function Request-ToolboxLease {
    $canonical = Resolve-ToolboxCheckoutRoot $RepoRoot
    $bytes = [Text.Encoding]::UTF8.GetBytes($canonical.ToUpperInvariant())
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 16) }
    finally { $sha.Dispose() }
    $name = "Local\gen-audio-toolbox-$digest"
    $script:LeaseMutex = New-Object Threading.Mutex($false, $name)
    if (-not $script:LeaseMutex.WaitOne(0)) {
        $script:LeaseMutex.Dispose()
        $script:LeaseMutex = $null
        throw "Another toolbox run owns the checkout lease for $RepoRoot"
    }
    $script:LeaseOwned = $true
    $script:LeaseDigest = $digest
    Write-ToolboxLog 'INFO' "Acquired checkout lease for $RepoRoot ($digest)."
}

function Complete-ToolboxLease {
    if ($script:LeaseOwned -and $script:LeaseMutex) {
        $script:LeaseMutex.ReleaseMutex()
        $script:LeaseOwned = $false
    }
    if ($script:LeaseMutex) {
        $script:LeaseMutex.Dispose()
        $script:LeaseMutex = $null
    }
}

function Add-BestEffortWarning {
    param([Parameter(Mandatory = $true)][string]$Message)
    $script:BestEffortWarnings.Add($Message)
    Write-ToolboxLog 'WARN' $Message
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
    catch { Add-BestEffortWarning "Transcript not started: $($_.Exception.Message)" }
}

function Complete-Logging {
    try { Stop-Transcript | Out-Null }
    catch { Add-BestEffortWarning "Transcript stop failed: $($_.Exception.Message)" }
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
    # Windows PowerShell 5.1 turns any native stderr line into a terminating
    # NativeCommandError under ErrorActionPreference=Stop (uv writes download
    # progress to stderr). The exit code is the failure signal, so log stderr.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = 0
        & $FilePath @Arguments 2>&1 | ForEach-Object {
            $line = if ($_ -is [Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
            if ($line) { Write-ToolboxLog 'CMD' $line }
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
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
    $scope = if ($Machine) { 'machine' } else { 'user' }
    if ($AcceptPackageAgreement -notcontains $Id) {
        throw ("$Id is required but not installed, and its package agreement has not been accepted for this run. " +
            "Review the license (winget show --id $Id --exact --source winget), then either install it yourself " +
            "(winget install --id $Id --exact --source winget --scope $scope) or rerun this toolbox with " +
            "-AcceptPackageAgreement $Id.")
    }
    $arguments = @(
        'install', '--id', $Id, '--exact', '--source', 'winget',
        '--silent', '--disable-interactivity',
        '--accept-source-agreements', '--accept-package-agreements'
    )
    try {
        try { Invoke-External $winget ($arguments + @('--scope', $scope) + $Extra) $RepoRoot }
        catch {
            # Some manifests (Rustlang.Rustup) publish no installer for an explicit
            # user scope. Retry once with the manifest's own default scope.
            if ($script:InstallerCancelledCodes -contains $global:LASTEXITCODE) {
                throw ("$Id was not installed: the installer was cancelled (exit $global:LASTEXITCODE), usually a declined " +
                    "or unattended UAC prompt. Run from an elevated prompt: winget install --id $Id --exact --source winget --scope $scope")
            }
            if ($Machine -or $global:LASTEXITCODE -ne $script:WingetNoApplicableInstaller) { throw }
            Write-ToolboxLog 'INFO' "$Id has no $scope-scope installer; retrying with the manifest default scope."
            Invoke-External $winget ($arguments + $Extra) $RepoRoot
        }
    }
    catch {
        if (-not (Get-Tool @($Id.Split('.')[-1]))) { throw }
        Add-BestEffortWarning "$Id reported an installation error, but its tool is now available; continuing in degraded mode."
    }
    Resolve-SessionPath
}

function Test-Checkout {
    $manifest = Join-Path $RepoRoot 'pyproject.toml'
    return (Test-Path -LiteralPath $manifest -PathType Leaf) -and
        (Select-String -LiteralPath $manifest -SimpleMatch 'name = "gen-audio"' -Quiet)
}

function Initialize-Repository {
    $script:RepoRoot = Resolve-ToolboxCheckoutRoot $RepoRoot
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
    $runtimeLibrary = Join-Path $RepoRoot 'scripts\lib\runtime-config.ps1'
    . $runtimeLibrary
    # Dot-sourcing inside this function scopes the library's functions to it, so
    # later steps (Write-RuntimeDescriptor) could not call them. Promote every
    # function the library defines to script scope.
    $libraryAst = [Management.Automation.Language.Parser]::ParseFile($runtimeLibrary, [ref]$null, [ref]$null)
    foreach ($definition in $libraryAst.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
        Set-Item -Path "function:script:$($definition.Name)" -Value (Get-Item -LiteralPath "function:$($definition.Name)").ScriptBlock
    }
    $script:RuntimeConfig = Read-GenAudioRuntimeConfig -RepoRoot $RepoRoot -NodePort $NodePort -McpPort $McpPort -PreviewPort $PreviewPort
    $script:NodePort = [int]$script:RuntimeConfig.ports.Node
    $script:McpPort = [int]$script:RuntimeConfig.ports.Mcp
    $script:PreviewPort = [int]$script:RuntimeConfig.ports.Preview
    $script:RuntimeDir = $script:RuntimeConfig.runtime
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
        $major = ([version]$NodeVersion).Major
        if ($actual -match "^$major\.\d+\.\d+$" -and [version]$actual -ge [version]$NodeVersion) {
            Write-ToolboxLog 'OK' "Node $actual -> $node"
            return $node
        }
        Write-ToolboxLog 'INFO' "Node $actual at $node is not Node $major >= $NodeVersion; using a user-level Node $NodeVersion."
    }
    $name = "node-v$NodeVersion-win-x64"
    $installDir = Join-Path $env:LOCALAPPDATA "gen-audio\toolbox\tools\$name"
    if (-not (Test-Path -LiteralPath (Join-Path $installDir 'node.exe') -PathType Leaf)) {
        $cache = Join-Path $script:RuntimeDir ("cache-node-" + [guid]::NewGuid().ToString('N'))
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
            try { Remove-Item -LiteralPath $cache -Recurse -Force -ErrorAction Stop }
            catch { Add-BestEffortWarning "Node cache cleanup failed for $cache`: $($_.Exception.Message)" }
        }
    }
    Add-PathPrefix $installDir
    $node = Join-Path $installDir 'node.exe'
    Write-ToolboxLog 'OK' "Node $NodeVersion -> $node"
    return $node
}

function Install-Uv {
    # uv is MIT/Apache-2.0 on PyPI and installs without a package agreement, so a
    # missing uv comes from pip before WinGet is considered.
    if (-not (Get-Tool @('uv.exe'))) {
        $python = Get-Tool @('python.exe')
        if ($python) {
            Invoke-External $python @('-m', 'pip', 'install', '--user', '--disable-pip-version-check', "uv>=$UvMinimumVersion") $RepoRoot
            $scripts = & $python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"
            if ($scripts) { Add-PathPrefix "$scripts".Trim() }
        }
    }
    if (-not (Get-Tool @('uv.exe'))) { Install-WingetPackage -Id 'astral-sh.uv' }
    $uv = Get-Tool @('uv.exe')
    $actual = ((& $uv --version) -replace '^uv\s+(\d+\.\d+\.\d+).*$', '$1').Trim()
    if ([version]$actual -lt [version]$UvMinimumVersion) {
        # An older uv resolves "3.14" to a pre-release interpreter (0.8.0 picks 3.14.0b4).
        Write-ToolboxLog 'INFO' "uv $actual is older than $UvMinimumVersion; updating it."
        try { Invoke-External $uv @('self', 'update') $RepoRoot }
        catch {
            $python = Get-Tool @('python.exe')
            if (-not $python) { throw "uv $actual is older than $UvMinimumVersion and could not update itself. Update uv, then rerun." }
            Invoke-External $python @('-m', 'pip', 'install', '--user', '--upgrade', '--disable-pip-version-check', "uv>=$UvMinimumVersion") $RepoRoot
        }
        $actual = ((& (Get-Tool @('uv.exe')) --version) -replace '^uv\s+(\d+\.\d+\.\d+).*$', '$1').Trim()
        if ([version]$actual -lt [version]$UvMinimumVersion) { throw "uv $actual on PATH is still older than $UvMinimumVersion." }
    }
    Write-ToolboxLog 'OK' "uv $actual"
}

# This entry point runs standalone (downloaded to %TEMP%) before a checkout
# exists, so it carries its own copy of the check in scripts\lib\devenv.ps1.
$script:MsvcBuildToolsInstallCommand = 'winget install --id Microsoft.VisualStudio.2022.BuildTools --exact --source winget --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"'

function Test-ToolboxMsvc {
    if (Get-Command cl.exe -ErrorAction SilentlyContinue) { return $true }
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) { return $false }
    $install = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    return ($LASTEXITCODE -eq 0 -and [bool]$install)
}

function Test-ToolboxElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-MsvcBuildToolsAvailable {
    # VS 2022 Build Tools are a machine-scope install that needs UAC. Check first,
    # before Node, uv or Rust are fetched, so an unelevated run without them stops
    # in seconds with the exact command instead of failing at the first link.
    if (Test-ToolboxMsvc) { return }
    $command = $script:MsvcBuildToolsInstallCommand
    if ($AcceptPackageAgreement -notcontains 'Microsoft.VisualStudio.2022.BuildTools') {
        throw ("Visual Studio 2022 Build Tools (C++ workload) are not installed; the Rust and Tauri builds need them. " +
            "From an elevated prompt run: $command, or rerun this toolbox elevated with -AcceptPackageAgreement Microsoft.VisualStudio.2022.BuildTools.")
    }
    if (-not (Test-ToolboxElevated)) {
        throw ("Visual Studio 2022 Build Tools need an elevated (UAC) install and this session is not elevated. " +
            "From an elevated prompt run: $command, then rerun the toolbox.")
    }
}

function Install-DependencySet {
    Write-ToolboxLog 'STEP' 'Installing system dependencies.'
    Assert-MsvcBuildToolsAvailable
    $git = Get-Tool @('git.exe')
    if (-not $git) { Install-WingetPackage -Id 'Git.Git' }
    $node = Install-Node
    if (-not (Get-Tool @('npm.cmd'))) { throw "npm was not found beside $node." }
    Install-Uv
    Invoke-External (Get-Tool @('uv.exe')) @('python', 'install', $PythonVersion) $RepoRoot
    if (-not (Get-Tool @('ffmpeg.exe')) -or -not (Get-Tool @('ffprobe.exe'))) {
        Install-WingetPackage -Id 'Gyan.FFmpeg'
        Add-PathPrefix (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links')
    }
    if (-not (Get-Tool @('cargo.exe'))) {
        Install-WingetPackage -Id 'Rustlang.Rustup'
        Add-PathPrefix (Join-Path $env:USERPROFILE '.cargo\bin')
        Invoke-External (Get-Tool @('rustup.exe')) @('default', 'stable') $RepoRoot
    }
    if (-not (Test-ToolboxMsvc)) {
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
            $download = Join-Path $script:RuntimeDir ("cache-" + [guid]::NewGuid().ToString('N') + "-" + $asset.Name)
            try {
                Write-ToolboxLog 'INFO' "Downloading $($asset.Name)"
                Invoke-WebRequest -UseBasicParsing -Uri $asset.Uri -OutFile $download -TimeoutSec 600
                if ((Get-FileSha256 $download) -ne $asset.Hash) {
                    throw "$($asset.Name) checksum mismatch."
                }
                Copy-Item -LiteralPath $download -Destination $destination -Force
            } finally {
                try { Remove-Item -LiteralPath $download -Force -ErrorAction Stop }
                catch { Add-BestEffortWarning "Model download cleanup failed for $download`: $($_.Exception.Message)" }
            }
        }
        if ((Get-Item -LiteralPath $destination).Length -lt 1) {
            throw "$($asset.Name) is empty."
        }
        Write-ToolboxLog 'OK' "$($asset.Name) bytes=$((Get-Item $destination).Length) sha256=$(Get-FileSha256 $destination)"
    }
}

function Build-Framework {
    Install-DependencySet
    Write-ToolboxLog 'STEP' 'Installing and building the full Gen-Audio framework.'
    Initialize-PackageNetwork
    $uv = Get-Tool @('uv.exe')
    $venv = Join-Path $RepoRoot '.venv'
    $python = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Invoke-External $uv @('venv', '--python', $PythonVersion, '--seed', $venv) $RepoRoot
    }
    $release = (& $python -c "import sys; print(sys.version_info.releaselevel, sys.version.split()[0])") -join ' '
    if ($release -notmatch '^final ') {
        throw "The checkout venv uses a pre-release Python ($release). Remove $venv and the pre-release interpreter (uv python uninstall <version>), then rerun."
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
    # uv resolves the lock while ignoring upper bounds on a dependency's
    # Requires-Python (kokoro-onnx 0.6.1 declares <3.14), and CI runs that lock on
    # 3.14 through uv sync. pip enforces the bound, so the same hash-pinned set
    # failed on a clean node; follow the lock's resolution instead.
    $pipArguments = @(
        '-m', 'pip', 'install', '--disable-pip-version-check',
        '--only-binary=:all:', '--require-hashes', '--ignore-requires-python',
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
    $configuration = Get-GenAudioRuntimeEnvironment $script:RuntimeConfig
    New-Item -ItemType Directory -Force -Path $script:RuntimeDir | Out-Null
    foreach ($entry in $configuration.GetEnumerator()) {
        Set-GenAudioProcessVariable -Name $entry.Key -Value "$($entry.Value)"
    }
    $legacyEnvironment = Join-Path $script:RuntimeDir 'node-environment.ps1'
    if (Test-Path -LiteralPath $legacyEnvironment) {
        Remove-Item -LiteralPath $legacyEnvironment -Force -ErrorAction Stop
    }
    $legacyText = Join-Path $script:RuntimeDir 'node-environment.txt'
    $descriptor = [ordered]@{
        checkout = $RepoRoot
        ports = $script:RuntimeConfig.ports
        paths = [ordered]@{
            runtime = $script:RuntimeDir
            models = $script:RuntimeConfig.models
            python = $script:RuntimeConfig.python
            mcpAddress = $script:RuntimeConfig.mcpAddressFile
        }
        environment = $configuration
        generatedAt = (Get-Date).ToUniversalTime().ToString('o')
    }
    $descriptorPath = $script:RuntimeConfig.descriptor
    $temporary = "$descriptorPath.$PID.tmp"
    try {
        [IO.File]::WriteAllText($temporary, ($descriptor | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $descriptorPath) {
            Remove-Item -LiteralPath $descriptorPath -Force -ErrorAction Stop
        }
        Move-Item -LiteralPath $temporary -Destination $descriptorPath
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            try { Remove-Item -LiteralPath $temporary -Force -ErrorAction Stop }
            catch { Add-BestEffortWarning "Descriptor temporary-file cleanup failed for $temporary`: $($_.Exception.Message)" }
        }
    }
    if (Test-Path -LiteralPath $legacyText) {
        Remove-Item -LiteralPath $legacyText -Force -ErrorAction Stop
    }
    $lines = @($configuration.GetEnumerator() | ForEach-Object {
        '$env:{0} = {1}' -f $_.Key, ([System.Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent("$($_.Value)") -replace '^', "'" -replace '$', "'")
    })
    [IO.File]::WriteAllLines($legacyText, $lines, (New-Object Text.UTF8Encoding($false)))
    Write-ToolboxLog 'OK' "Persisted checkout-scoped Gen-Audio descriptor: $descriptorPath"
}

function Test-ServiceIdentity {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Node', 'Mcp', 'Preview')][string]$Service,
        [Parameter(Mandatory = $true)][string]$Uri
    )
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
        if ($response.StatusCode -ne 200) { return $false }
        if ($Service -eq 'Preview') { return "$($response.Content)" -match '<title>Darbot Gen-Audio</title>' }
        $body = $response.Content | ConvertFrom-Json
        $expected = if ($Service -eq 'Node') { 'genaid-audio' } else { 'gen-audio-mcp' }
        return $body.service -eq $expected
    } catch {
        return $false
    }
}

function Test-LauncherOwnership {
    param([Parameter(Mandatory = $true)][string]$PidPath, [Parameter(Mandatory = $true)][string]$ExpectedService)
    if (-not (Test-Path -LiteralPath $PidPath -PathType Leaf)) { return $false }
    $recorded = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $PidPath -Raw).Trim(), [ref]$recorded)) { return $false }
    $record = Get-CimInstance Win32_Process -Filter "ProcessId=$recorded" -ErrorAction SilentlyContinue
    if (-not $record) { return $false }
    $expectedRun = Join-Path $RepoRoot 'scripts\run.ps1'
    return $record.CommandLine -like "*$expectedRun*" -and $record.CommandLine -like "*$ExpectedService*"
}

function Test-HealthOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$PidPath,
        [Parameter(Mandatory = $true)][string]$Uri
    )
    $launcherPid = 0
    if (-not (Test-Path -LiteralPath $PidPath) -or
        -not [int]::TryParse((Get-Content -LiteralPath $PidPath -Raw).Trim(), [ref]$launcherPid)) { return $false }
    try {
        $body = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2 |
            Select-Object -ExpandProperty Content | ConvertFrom-Json
        $reportedPid = [int]$body.pid
        if ($reportedPid -lt 1) { return $false }
        $current = $reportedPid
        for ($depth = 0; $depth -lt 8; $depth++) {
            if ($current -eq $launcherPid) { return $true }
            $record = Get-CimInstance Win32_Process -Filter "ProcessId=$current" -ErrorAction SilentlyContinue
            if (-not $record -or -not $record.ParentProcessId) { break }
            $current = [int]$record.ParentProcessId
        }
    } catch {
        return $false
    }
    return $false
}

function Get-ToolboxProcessTreeId {
    param([Parameter(Mandatory = $true)][int]$RootProcessId)
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $depths = @{}
    $depths[$RootProcessId] = 0
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($record in $processes) {
            $parent = [int]$record.ParentProcessId
            $processId = [int]$record.ProcessId
            if ($depths.ContainsKey($parent) -and -not $depths.ContainsKey($processId)) {
                $depths[$processId] = $depths[$parent] + 1
                $changed = $true
            }
        }
    }
    return @($depths.Keys | Sort-Object { $depths[$_] } -Descending)
}

function Complete-ToolboxProcessTree {
    param(
        [Parameter(Mandatory = $true)][int]$RootProcessId,
        [int[]]$AdditionalProcessIds = @()
    )
    $ids = @(Get-ToolboxProcessTreeId -RootProcessId $RootProcessId)
    foreach ($processId in $AdditionalProcessIds) {
        if ($processId -gt 0 -and $ids -notcontains $processId) { $ids += $processId }
    }
    $errors = New-Object System.Collections.Generic.List[string]
    foreach ($processId in ($ids | Select-Object -Unique)) {
        if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
            try { Stop-Process -Id $processId -Force -ErrorAction Stop }
            catch { $errors.Add("failed to stop PID $processId`: $($_.Exception.Message)") }
        }
    }
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        $alive = @($ids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($alive.Count -eq 0) { break }
        Start-Sleep -Milliseconds 100
    }
    $alive = @($ids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    foreach ($processId in $alive) { $errors.Add("PID $processId survived full process-tree termination") }
    if ($errors.Count) { throw ($errors -join '; ') }
}

function Test-ToolboxPortReleased {
    param([Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port)
    $listener = $null
    try {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
        $listener.Server.ExclusiveAddressUse = $true
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) {
            try { $listener.Stop() }
            catch { $null = $_ }
        }
    }
}

function Wait-ToolboxPortReleased {
    param(
        [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port,
        [int]$TimeoutSeconds = 10
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-ToolboxPortReleased -Port $Port) { return }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    throw "port 127.0.0.1:$Port is still owned after launcher process-tree termination"
}

function Invoke-NodeService {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Node', 'Mcp', 'Preview')][string]$Service,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$HealthUrl
    )
    $serviceName = $Service.ToLowerInvariant()
    $pidPath = Join-Path $script:RuntimeDir "$serviceName.pid"
    if (Test-ServiceIdentity -Service $Service -Uri $HealthUrl) {
        if (-not (Test-LauncherOwnership -PidPath $pidPath -ExpectedService $Service)) {
            throw "$Service identity matched at $HealthUrl, but no live toolbox-owned launcher PID is recorded; refusing to adopt an unknown process."
        }
        if ($Service -in @('Node', 'Mcp') -and -not (Test-HealthOwnership -PidPath $pidPath -Uri $HealthUrl)) {
            throw "$Service identity matched at $HealthUrl, but its reported PID is not owned by the recorded launcher."
        }
        Write-ToolboxLog 'OK' "$Service is healthy at $HealthUrl and owned by this checkout."
        return
    }
    if (Test-Path -LiteralPath $pidPath) {
        $recorded = [int](Get-Content -LiteralPath $pidPath -Raw)
        $existing = Get-Process -Id $recorded -ErrorAction SilentlyContinue
        if ($existing) {
            $record = Get-CimInstance Win32_Process -Filter "ProcessId=$recorded" -ErrorAction SilentlyContinue
            $expectedRun = Join-Path $RepoRoot 'scripts\run.ps1'
            if ($record -and $record.CommandLine -like "*$expectedRun*" -and $record.CommandLine -like "*$Service*") {
                Complete-ToolboxProcessTree -RootProcessId $existing.Id
                Wait-ToolboxPortReleased -Port $Port
            } else {
                Add-BestEffortWarning "PID $recorded was reused by another process; stale $serviceName PID file will be replaced."
            }
        }
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction Stop
    }
    $powershell = Get-Tool @('pwsh.exe', 'powershell.exe')
    $run = Join-Path $RepoRoot 'scripts\run.ps1'
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $run, '-Service', $Service, '-Port', "$Port")
    $process = Start-Process -FilePath $powershell -ArgumentList $arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
    [IO.File]::WriteAllText($pidPath, "$($process.Id)", (New-Object Text.UTF8Encoding($false)))
    $trackedChildIds = New-Object System.Collections.Generic.HashSet[int]
    try {
        $deadline = (Get-Date).AddSeconds(60)
        while ((Get-Date) -lt $deadline) {
            try {
                foreach ($processId in (Get-ToolboxProcessTreeId -RootProcessId $process.Id)) {
                    if ($processId -ne $process.Id) { $trackedChildIds.Add($processId) | Out-Null }
                }
            } catch {
                Add-BestEffortWarning "Could not snapshot launcher descendants for PID $($process.Id): $($_.Exception.Message)"
            }
            if ($process.HasExited) {
                throw "$Service exited with $($process.ExitCode). See $script:RuntimeDir."
            }
            if (Test-ServiceIdentity -Service $Service -Uri $HealthUrl) {
                if ($Service -in @('Node', 'Mcp') -and -not (Test-HealthOwnership -PidPath $pidPath -Uri $HealthUrl)) {
                    throw "$Service at $HealthUrl reports a PID outside launcher $($process.Id)'s process tree."
                }
                Write-ToolboxLog 'OK' "$Service healthy at $HealthUrl (launcher PID $($process.Id))."
                return
            }
            Start-Sleep -Milliseconds 500
        }
        throw "$Service did not become healthy at $HealthUrl."
    } catch {
        $startupError = $_.Exception.Message
        $cleanupErrors = New-Object System.Collections.Generic.List[string]
        try {
            Complete-ToolboxProcessTree -RootProcessId $process.Id `
                -AdditionalProcessIds @($trackedChildIds)
        }
        catch { $cleanupErrors.Add("failed to stop launcher process tree $($process.Id): $($_.Exception.Message)") }
        try { Wait-ToolboxPortReleased -Port $Port }
        catch { $cleanupErrors.Add($_.Exception.Message) }
        if (Test-Path -LiteralPath $pidPath) {
            try { Remove-Item -LiteralPath $pidPath -Force -ErrorAction Stop }
            catch { $cleanupErrors.Add("failed to remove $pidPath`: $($_.Exception.Message)") }
        }
        if ($cleanupErrors.Count) { $startupError += '; cleanup errors: ' + ($cleanupErrors -join '; ') }
        throw $startupError
    }

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
    if (-not $ready.ready -or $ready.service -ne 'genaid-audio') { throw 'Node readiness check failed or returned the wrong service.' }
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
    if (-not (Test-ServiceIdentity -Service Preview -Uri "http://127.0.0.1:$PreviewPort/")) { throw 'Preview is not reachable or is not the Gen-Audio UI.' }
    foreach ($item in @(@('Node', $NodePort), @('Mcp', $McpPort), @('Preview', $PreviewPort))) {
        $pidPath = Join-Path $script:RuntimeDir "$($item[0].ToLowerInvariant()).pid"
        if (-not (Test-LauncherOwnership -PidPath $pidPath -ExpectedService $item[0])) {
            throw "$($item[0]) on port $($item[1]) is healthy but is not owned by a current toolbox launcher."
        }
    }
    $nodePid = Join-Path $script:RuntimeDir 'node.pid'
    $mcpPid = Join-Path $script:RuntimeDir 'mcp.pid'
    if (-not (Test-HealthOwnership -PidPath $nodePid -Uri "http://127.0.0.1:$NodePort/genaid-audio/health")) {
        throw 'Node health PID is not in the checkout launcher process tree.'
    }
    if (-not (Test-HealthOwnership -PidPath $mcpPid -Uri "http://127.0.0.1:$McpPort/health")) {
        throw 'MCP health PID is not in the checkout launcher process tree.'
    }
    Write-ToolboxLog 'OK' "Node=$NodePort MCP=$McpPort Preview=$PreviewPort; all health checks passed."
}

function Test-Framework {
    if ($SkipTests) {
        $script:ValidationSkipped = $true
        Add-BestEffortWarning 'Framework validation was explicitly skipped by -SkipTests; toolbox result is degraded.'
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
        Set-GenAudioProcessVariable -Name $name -Value $null
    }
    # The gate runs while this toolbox holds the checkout lease; tell the lease
    # test so it checks against this holder instead of starting its own.
    $savedLease = [Environment]::GetEnvironmentVariable('GEN_AUDIO_TOOLBOX_LEASE', 'Process')
    Set-GenAudioProcessVariable -Name 'GEN_AUDIO_TOOLBOX_LEASE' -Value $script:LeaseDigest
    try {
        Invoke-External $powershell @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', (Join-Path $RepoRoot 'scripts\test.ps1'),
            '-Tag', 'node-bootstrap', '-FromLock',
            '-Python', (Join-Path $RepoRoot '.venv\Scripts\python.exe')
        ) $RepoRoot
    } finally {
        Set-GenAudioProcessVariable -Name 'GEN_AUDIO_TOOLBOX_LEASE' -Value $savedLease
        foreach ($name in $names) {
            Set-GenAudioProcessVariable -Name $name -Value $saved[$name]
        }
    }
}

function Invoke-Toolbox {
    $exitCode = 0
    $loggingStarted = $false
    try {
        if ($env:OS -ne 'Windows_NT') { throw 'The Windows node toolbox requires Windows.' }
        Resolve-ToolboxRepositoryTarget
        Request-ToolboxLease
        Initialize-Logging
        $loggingStarted = $true
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
        if ($script:ValidationSkipped -or $script:BestEffortWarnings.Count -gt 0) {
            $exitCode = 2
            Write-ToolboxLog 'WARN' "Gen-Audio toolbox ended DEGRADED ($($script:BestEffortWarnings.Count) warning(s)); this is not a full success."
        } else {
            Write-ToolboxLog 'OK' "Gen-Audio toolbox completed in $([math]::Round(((Get-Date) - $script:RunStartedAt).TotalMinutes, 1)) minutes."
        }
    } catch {
        $exitCode = 1
        Write-ToolboxLog 'ERROR' "$($_.Exception.Message)"
        Write-ToolboxLog 'ERROR' "Bootstrap failed; inspect $script:LogPath and $script:RuntimeDir."
    } finally {
        try { Complete-ToolboxLease }
        catch {
            $exitCode = 1
            Write-ToolboxLog 'ERROR' "Checkout lease cleanup failed: $($_.Exception.Message)"
        }
        if ($loggingStarted) { Complete-Logging }
        if ($exitCode -eq 0 -and $script:BestEffortWarnings.Count -gt 0) { $exitCode = 2 }
        Write-Output "TOOLBOX_EXIT $exitCode"
        if ($script:LogPath) { Write-Output "Log: $script:LogPath" }
    }
    exit $exitCode
}

Invoke-Toolbox
