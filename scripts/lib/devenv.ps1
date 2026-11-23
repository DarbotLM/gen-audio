# Shared helpers for the canonical scripts. Dot-source it; it is not an entry point.
#   . (Join-Path $PSScriptRoot 'lib\devenv.ps1')
# Works on Windows PowerShell 5.1 and PowerShell 7.
#
# This is the only file allowed to import the MSVC environment (vcvars64).
# The sprawl gate in scripts/test.ps1 fails if any other script does.

Set-StrictMode -Version 2.0

function Write-Step {
    # Console progress for a person watching the run. Results that other
    # tools read (BUILD_OK, TEST_SUMMARY, sha256 lines) go to the pipeline.
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Progress text for the console only; never parsed.')]
    [CmdletBinding()]
    param([Parameter(Mandatory = $true, Position = 0)][string]$Message)
    Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] $Message"
}

function Invoke-Checked {
    # Run a native command and throw on a non-zero exit code.
    # A simple function on purpose: it forwards $args untouched. An advanced
    # function (param block) binds common parameters, so on Windows
    # PowerShell 5.1 `cargo build -p x` lost `-p` to -PipelineVariable and
    # on PowerShell 7.4+ it threw. Fix from Darbot's PR #3.
    $file = [string]$args[0]
    $commandArgs = [string[]]@($args | Select-Object -Skip 1)
    # Native stderr (cargo progress) must not become a terminating error
    # when a caller redirects it under $ErrorActionPreference = 'Stop'.
    $ErrorActionPreference = 'Continue'
    # Through the host (stderr as plain text), so Start-Transcript records it:
    # Windows PowerShell 5.1 does not transcribe native output written
    # straight to the console.
    & $file @commandArgs 2>&1 | ForEach-Object { "$_" } | Out-Host
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "$file $($commandArgs -join ' ') failed with exit $code"
    }
}

function Get-Sha256 {
    # Lower-case hex SHA-256 of a file, straight from .NET. Get-FileHash is a
    # script function in Windows PowerShell 5.1's Utility module, and it goes
    # missing when 5.1 inherits PowerShell 7's PSModulePath (Start-Process
    # from pwsh 7 does that), so the canonical scripts do not depend on it.
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    $full = (Resolve-Path -LiteralPath $LiteralPath).ProviderPath
    $stream = [System.IO.File]::Open($full, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash($stream)
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-LibraryWavProblem {
    # The gitignored library WAVs against schemas/asset-object/media.lock.json:
    # one line per WAV that is missing, has the wrong byte count, or the wrong
    # sha256 (WAV_MISSING / WAV_BYTES / WAV_MISMATCH). No output means all
    # match. Shared by build-tauri-windows.ps1 (a release must embed the real
    # WAVs) and test.ps1 -WithWav. A gate that passes by skipping is not a gate,
    # so an empty or unreadable lock is an error too.
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory = $true)][string]$Root)
    $lockPath = Join-Path $Root 'schemas\asset-object\media.lock.json'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw "media.lock.json is missing: $lockPath" }
    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
    $names = @(if ($lock.PSObject.Properties['media']) { $lock.media.PSObject.Properties | ForEach-Object { $_.Name } })
    if ($names.Count -eq 0) { throw "$lockPath lists no WAVs" }
    $library = Join-Path $Root 'apps\desktop\public\library'
    foreach ($name in $names) {
        $entry = $lock.media.$name
        $path = Join-Path $library $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            "WAV_MISSING $name (stage it in apps/desktop/public/library; *.wav is gitignored)"
            continue
        }
        $bytes = (Get-Item -LiteralPath $path).Length
        if ($entry.PSObject.Properties['bytes'] -and [int64]$entry.bytes -ne $bytes) {
            "WAV_BYTES $name has $bytes bytes, media.lock.json $($entry.bytes)"
            continue
        }
        $got = Get-Sha256 -LiteralPath $path
        if ($got -ne [string]$entry.sha256) { "WAV_MISMATCH $name sha256 $got, media.lock.json $($entry.sha256)" }
    }
}

function Import-LibraryWav {
    # Stage the media.lock.json WAVs from -From (another node's verified copy,
    # a share, or SMAX D:\gen-audio\artifacts\library) into
    # apps\desktop\public\library. Only a file whose byte count and sha256
    # match the lock is copied; anything else is reported and left out, so
    # Get-LibraryWavProblem after it still refuses. One line per WAV:
    # WAV_STAGED / WAV_PRESENT / WAV_SOURCE_MISSING / WAV_SOURCE_MISMATCH.
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$From)
    if (-not (Test-Path -LiteralPath $From -PathType Container)) { throw "-WavSource $From is not a folder" }
    $lockPath = Join-Path $Root 'schemas\asset-object\media.lock.json'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw "media.lock.json is missing: $lockPath" }
    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
    $names = @(if ($lock.PSObject.Properties['media']) { $lock.media.PSObject.Properties | ForEach-Object { $_.Name } })
    if ($names.Count -eq 0) { throw "$lockPath lists no WAVs" }
    $library = Join-Path $Root 'apps\desktop\public\library'
    New-Item -ItemType Directory -Force -Path $library | Out-Null
    foreach ($name in $names) {
        $want = ([string]$lock.media.$name.sha256).ToLowerInvariant()
        $dest = Join-Path $library $name
        if ((Test-Path -LiteralPath $dest -PathType Leaf) -and (Get-Sha256 -LiteralPath $dest) -eq $want) {
            "WAV_PRESENT $name"
            continue
        }
        $src = Join-Path $From $name
        if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
            "WAV_SOURCE_MISSING $name (not in $From)"
            continue
        }
        $got = Get-Sha256 -LiteralPath $src
        if ($got -ne $want) {
            "WAV_SOURCE_MISMATCH $name sha256 $got, media.lock.json $want (not copied)"
            continue
        }
        Copy-Item -LiteralPath $src -Destination $dest -Force
        if ((Get-Sha256 -LiteralPath $dest) -ne $want) { throw "copy of $name did not keep its bytes" }
        "WAV_STAGED $name sha256 $want"
    }
}

function Select-CargoBinArtifact {
    # From `cargo build --message-format=json*` stdout lines, the
    # compiler-artifact cargo reported for binary $Bin: its executable path and
    # whether cargo's fingerprint said it was already up to date (fresh). That
    # is cargo's own verdict on staleness; file mtimes are not (a no-op
    # rebuild leaves the old mtime in place).
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][AllowEmptyString()][string[]]$Line, [Parameter(Mandatory = $true)][string]$Bin)
    $found = $null
    foreach ($text in $Line) {
        if (-not $text -or -not $text.StartsWith('{')) { continue }
        $message = $text | ConvertFrom-Json
        if (-not $message.PSObject.Properties['reason'] -or $message.reason -ne 'compiler-artifact') { continue }
        if ($message.target.name -ne $Bin -or @($message.target.kind) -notcontains 'bin') { continue }
        if (-not $message.PSObject.Properties['executable'] -or -not $message.executable) { continue }
        $found = [pscustomobject]@{ Executable = [string]$message.executable; Fresh = [bool]$message.fresh }
    }
    if (-not $found) { throw "cargo reported no compiler-artifact for bin $Bin, so there is no way to tell which binary it built" }
    return $found
}

function Invoke-CargoBinBuild {
    # cargo build one binary and return Select-CargoBinArtifact's answer
    # (Executable, Fresh). Throws on a non-zero exit. Diagnostics still go to
    # the console (json-render-diagnostics); stdout carries the JSON messages.
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param([Parameter(Mandatory = $true)][string]$Package, [Parameter(Mandatory = $true)][string]$Bin, [string[]]$ExtraArgs = @())
    $cargoArgs = @('build', '-p', $Package, '--bin', $Bin, '--message-format=json-render-diagnostics') + $ExtraArgs
    $ErrorActionPreference = 'Continue'
    # stdout is cargo's JSON; stderr (diagnostics, progress) goes through the
    # host as text so a build transcript records it.
    $lines = New-Object System.Collections.Generic.List[string]
    & cargo @cargoArgs 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { "$_" | Out-Host } else { $lines.Add([string]$_) }
    }
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "cargo $($cargoArgs -join ' ') failed with exit $code" }
    return Select-CargoBinArtifact -Line $lines.ToArray() -Bin $Bin
}

function Get-CargoUnit {
    # cargo's per-unit build directories for package $Package under a profile
    # dir such as target\release, in either layout cargo writes:
    #   legacy   .fingerprint\<pkg>-<hash>\ (fingerprint files),
    #            build\<pkg>-<hash>\output (build-script stdout), artifacts
    #            flat in deps\ as <crate>-<hash>.<ext>.
    #   current  build\<pkg>\<hash>\fingerprint\, build\<pkg>\<hash>\out\
    #            (the unit's artifacts, no hash suffix) and, for a build-script
    #            run, build\<pkg>\<hash>\run\stdout. cargo 1.100 writes only
    #            this layout: there is no .fingerprint or deps dir at all.
    # One object per unit: Layout, Hash, Fingerprint (dir), FingerprintFiles
    # (file names in it), Out (artifact dir), BuildScriptOutput (path; may
    # not exist).
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param([Parameter(Mandatory = $true)][string]$ProfileDir, [Parameter(Mandatory = $true)][string]$Package)
    $legacyHashes = New-Object System.Collections.Generic.SortedSet[string]
    foreach ($parent in @((Join-Path $ProfileDir '.fingerprint'), (Join-Path $ProfileDir 'build'))) {
        foreach ($dir in @(Get-ChildItem -LiteralPath $parent -Directory -Filter "$Package-*" -ErrorAction SilentlyContinue)) {
            $hash = $dir.Name.Substring($Package.Length + 1)
            if ($hash -match '^[0-9a-f]{16}$') { $legacyHashes.Add($hash) | Out-Null }
        }
    }
    foreach ($hash in $legacyHashes) {
        $fingerprint = Join-Path $ProfileDir ".fingerprint\$Package-$hash"
        [pscustomobject]@{
            Layout = 'legacy'; Hash = $hash; Fingerprint = $fingerprint
            FingerprintFiles = @(Get-ChildItem -LiteralPath $fingerprint -File -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
            Out = (Join-Path $ProfileDir 'deps'); BuildScriptOutput = (Join-Path $ProfileDir "build\$Package-$hash\output")
        }
    }
    foreach ($dir in @(Get-ChildItem -LiteralPath (Join-Path $ProfileDir "build\$Package") -Directory -ErrorAction SilentlyContinue)) {
        if ($dir.Name -notmatch '^[0-9a-f]{16}$') { continue }
        $fingerprint = Join-Path $dir.FullName 'fingerprint'
        [pscustomobject]@{
            Layout = 'current'; Hash = $dir.Name; Fingerprint = $fingerprint
            FingerprintFiles = @(Get-ChildItem -LiteralPath $fingerprint -File -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
            Out = (Join-Path $dir.FullName 'out'); BuildScriptOutput = (Join-Path $dir.FullName 'run\stdout')
        }
    }
}

function Get-SevenZip {
    # 7z.exe on PATH or in Program Files; the installer payload check needs it.
    [CmdletBinding()]
    [OutputType([string])]
    param()
    $tool = Get-Command 7z.exe -ErrorAction SilentlyContinue
    if ($tool) { return $tool.Source }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base) { continue }
        $candidate = Join-Path $base '7-Zip\7z.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Compare-TauriBundleExe {
    # The installer's payload exe against the built exe, byte for byte. After
    # cargo links the exe, tauri's bundler rewrites it in place once per bundle
    # (Patching ... with bundle type information: nsis, then msi): it sets the
    # three ASCII letters after __TAURI_BUNDLE_TYPE_VAR_ (UNK, NSS, MSI, ...).
    # So target\release\gen-audio.exe changes again after makensis runs, and
    # its sha256 is never the installer's. This returns where the two differ
    # only in that marker, with both values, and throws on any other byte.
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param([Parameter(Mandatory = $true)][string]$Built, [Parameter(Mandatory = $true)][string]$Payload)
    $marker = '__TAURI_BUNDLE_TYPE_VAR_'
    $a = [IO.File]::ReadAllBytes($Built)
    $b = [IO.File]::ReadAllBytes($Payload)
    if ($a.Length -ne $b.Length) { throw "installer payload $Payload is $($b.Length) bytes, the built exe $Built is $($a.Length)" }
    $latin = [Text.Encoding]::GetEncoding(28591)
    $text = $latin.GetString($a)
    $offset = $text.IndexOf($marker, [StringComparison]::Ordinal)
    if ($offset -lt 0 -or $text.IndexOf($marker, $offset + 1, [StringComparison]::Ordinal) -ge 0) {
        throw "$Built must hold exactly one $marker marker"
    }
    $at = $offset + $marker.Length
    $builtType = $latin.GetString($a, $at, 3)
    $payloadType = $latin.GetString($b, $at, 3)
    foreach ($value in $builtType, $payloadType) {
        if ($value -cnotmatch '^[A-Z]{3}$') { throw "bundle type marker '$value' at offset $at is not three capital letters" }
    }
    $normalized = [byte[]]$b.Clone()
    [Array]::Copy($a, $at, $normalized, $at, 3)
    $same = [Security.Cryptography.SHA256]::HashData($a)
    $other = [Security.Cryptography.SHA256]::HashData($normalized)
    if (-not [Linq.Enumerable]::SequenceEqual($same, $other)) {
        throw "installer payload $Payload differs from the built exe $Built beyond the tauri bundle-type marker: the installer carries another link of the exe"
    }
    return [pscustomobject]@{
        Offset = $at; BuiltType = $builtType; PayloadType = $payloadType
        PayloadSha256 = (Get-Sha256 -LiteralPath $Payload); Identical = ($builtType -ceq $payloadType)
    }
}

function Get-OutputFingerprint {
    # Length and sha256 of a build output, or $null when it is missing.
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) { return $null }
    $item = Get-Item -LiteralPath $LiteralPath
    return [pscustomobject]@{ Path = $item.FullName; Length = $item.Length; Sha256 = (Get-Sha256 -LiteralPath $item.FullName) }
}

function Assert-BuildOutput {
    # A build output judged by content, never by mtime (a no-op rebuild does
    # not rewrite an up-to-date output, so its old mtime says nothing). It must
    # exist after the build step, which is exit-code checked. Its sha256 is
    # compared with the Get-OutputFingerprint taken before that step: State is
    # 'rebuilt' (new or changed bytes) or 'unchanged' (same bytes, up to date).
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param([Parameter(Mandatory = $true)][string]$LiteralPath, [Parameter(Mandatory = $true)][string]$What, [AllowNull()]$Before)
    $after = Get-OutputFingerprint -LiteralPath $LiteralPath
    if (-not $after) { throw "$What is missing after the build: $LiteralPath" }
    $state = if ($Before -and $Before.Sha256 -eq $after.Sha256) { 'unchanged' } else { 'rebuilt' }
    return ($after | Add-Member -NotePropertyName State -NotePropertyValue $state -PassThru)
}

function Get-DistCopyDrift {
    # Files under $Source (apps/desktop/public) that the build should have
    # copied into $Dist byte for byte: one DIST_MISSING or DIST_CHANGED line
    # (path relative to $Source, forward slashes) per file that it did not.
    # No output means dist holds this tree's public files.
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory = $true)][string]$Source, [Parameter(Mandatory = $true)][string]$Dist)
    $base = (Resolve-Path -LiteralPath $Source).ProviderPath.TrimEnd('\', '/')
    foreach ($file in @(Get-ChildItem -LiteralPath $base -Recurse -File)) {
        $rel = $file.FullName.Substring($base.Length).TrimStart('\', '/') -replace '\\', '/'
        $copy = Join-Path $Dist $rel
        if (-not (Test-Path -LiteralPath $copy -PathType Leaf)) { "DIST_MISSING $rel"; continue }
        if ((Get-Item -LiteralPath $copy).Length -ne $file.Length -or (Get-Sha256 -LiteralPath $copy) -ne (Get-Sha256 -LiteralPath $file.FullName)) {
            "DIST_CHANGED $rel"
        }
    }
}

function Get-BuildLogPath {
    # Where a build-tauri-windows.ps1 run saves its transcript:
    # <Root>/target/logs/build-<12-char HEAD sha>-<yyyyMMdd-HHmmss>.log
    # ("nogit" in place of the sha outside a git checkout).
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory = $true)][string]$Root)
    $ErrorActionPreference = 'Continue'
    $sha = "$(& git -C $Root rev-parse --short=12 HEAD 2>$null)".Trim()
    if ($LASTEXITCODE -ne 0 -or -not $sha) { $sha = 'nogit' }
    $name = 'build-{0}-{1}.log' -f $sha, (Get-Date -Format 'yyyyMMdd-HHmmss')
    return (Join-Path (Join-Path (Join-Path $Root 'target') 'logs') $name)
}

function Test-IsWindowsHost {
    [CmdletBinding()]
    [OutputType([bool])]
    param()
    return ($env:OS -eq 'Windows_NT')
}

# The one supported way to get the MSVC linker and Windows SDK that Rust's
# x86_64-pc-windows-msvc target and Tauri need. It is a machine-scope install,
# so it needs an elevated (UAC) prompt; scripts print it instead of running it.
$script:MsvcBuildToolsInstallCommand = 'winget install --id Microsoft.VisualStudio.2022.BuildTools --exact --source winget --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"'

function Get-MsvcBuildToolsInstallCommand {
    [CmdletBinding()]
    [OutputType([string])]
    param()
    return $script:MsvcBuildToolsInstallCommand
}

function Test-MsvcBuildTool {
    # True when cl.exe is on PATH or vswhere finds the x64 C++ tools.
    [CmdletBinding()]
    [OutputType([bool])]
    param()
    if (Get-Command cl.exe -ErrorAction SilentlyContinue) { return $true }
    $installer = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { return $false }
    $vsPath = & $installer -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    return ($LASTEXITCODE -eq 0 -and [bool]$vsPath)
}

function Assert-NodeMajor {
    # Fails early, with the version found, unless node on PATH is the given major.
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$Major)
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        throw "Node.js $Major is required on PATH. Install the user-level zip from https://nodejs.org/dist/latest-v$Major.x/ (no UAC needed) or run scripts\toolbox.ps1 -Step Deps."
    }
    $version = ((& $node.Source --version) -replace '^v', '').Trim()
    if ($version -notmatch "^$Major\.\d+\.\d+$") {
        throw "Node.js $Major is required; $($node.Source) is v$version. Put Node $Major first on PATH or run scripts\toolbox.ps1 -Step Deps."
    }
    Write-Step "node v$version ($($node.Source))"
}

function Import-VsDevEnv {
    # Import the x64 MSVC build environment (cl.exe, link.exe, INCLUDE, LIB)
    # into this PowerShell session from vcvars64.bat.
    # Skips when cl.exe is already on PATH. Throws when Visual Studio or the
    # C++ build tools are missing; it never guesses a path.
    [CmdletBinding()]
    param()
    if (-not (Test-IsWindowsHost)) {
        Write-Verbose 'Import-VsDevEnv: not Windows, nothing to import.'
        return
    }
    if (Get-Command cl.exe -ErrorAction SilentlyContinue) {
        Write-Step 'MSVC environment already present (cl.exe on PATH); skipping vcvars64'
        return
    }
    $installer = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $installer)) {
        throw ("vswhere.exe not found at $installer, so Visual Studio 2022 Build Tools (C++ workload) are not installed. " +
            "From an elevated prompt run: $(Get-MsvcBuildToolsInstallCommand)")
    }
    $vsPath = & $installer -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0 -or -not $vsPath) {
        throw ("vswhere found no Visual Studio install with the x64 C++ tools (Microsoft.VisualStudio.Component.VC.Tools.x86.x64). " +
            "From an elevated prompt run: $(Get-MsvcBuildToolsInstallCommand)")
    }
    $vcvars = Join-Path ([string]@($vsPath)[0]) 'VC\Auxiliary\Build\vcvars64.bat'
    if (-not (Test-Path -LiteralPath $vcvars)) {
        throw "vcvars64.bat not found at $vcvars"
    }
    Write-Step "import MSVC environment from $vcvars"
    $lines = & cmd.exe /d /s /c "`"$vcvars`" >nul && set"
    if ($LASTEXITCODE -ne 0) {
        throw "vcvars64.bat failed with exit $LASTEXITCODE"
    }
    foreach ($line in $lines) {
        if ($line -match '^([^=]+)=(.*)$') {
            Set-Item -LiteralPath "Env:$($Matches[1])" -Value $Matches[2]
        }
    }
    if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
        throw 'vcvars64.bat ran but cl.exe is still not on PATH.'
    }
}
