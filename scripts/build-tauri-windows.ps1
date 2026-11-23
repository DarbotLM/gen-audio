<#
.SYNOPSIS
Release-build Gen-Audio for Windows. This is the only supported build path.

.DESCRIPTION
-Mode Full (default): NSIS and MSI installers that contain gen-audio.exe (tray
app), the gen-audio-mcp sidecar and the gen-audio-acp agent, plus an MSIX and
a tools NuGet package of the same verified binaries
(scripts\pack-windows-msix-nuget.ps1). Packages stay unsigned unless
-CertificateThumbprint names an existing code-signing certificate.
-Mode NoBundle: `tauri build --no-bundle`. Same release exe with the embedded
frontendDist, no installers. Faster, for local checks.

Both modes go through the tauri CLI (pinned), so the release exe always has
the custom-protocol feature and loads the embedded UI, never devUrl.
Every native step is exit-code checked. Before any build step, every library
WAV named in schemas/asset-object/media.lock.json must be staged with the
locked sha256 and byte count (they are gitignored, and the release embeds
them). The script prints exactly one BUILD_OK line, and only after the
outputs are verified by content (sha256, never mtime). Every run, failed ones included,
saves its full transcript to target/logs/build-<sha>-<timestamp>.log; the
first output line (BUILD_LOG) and BUILD_OK (log=) name it.

Works on Windows PowerShell 5.1 and PowerShell 7.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-tauri-windows.ps1 -Mode Full
pwsh -NoProfile -File scripts/build-tauri-windows.ps1 -Mode NoBundle
#>
[CmdletBinding()]
param(
    [ValidateSet('Full', 'NoBundle')]
    [string]$Mode = 'Full',
    [string]$CertificateThumbprint
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'lib\devenv.ps1')

# The CLI is installed by npm ci from the exact devDependency in apps/desktop.
$TauriCli = 'tauri'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'lib\runtime-config.ps1')
$runtimeConfig = Read-GenAudioRuntimeConfig -RepoRoot $root
$desktop = Join-Path $root 'apps\desktop'
$srcTauri = Join-Path $desktop 'src-tauri'
$releaseDir = Join-Path $root 'target\release'
Set-Location $root

function Get-FreeLoopbackPort {
    $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), 0
    $listener.Start()
    try { return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}

function Invoke-Checked {
    # Simple function on purpose. A param() block with [Parameter()] is an
    # advanced function, so PowerShell binds a single-dash token itself:
    # -p is -PipelineVariable on Windows PowerShell 5.1 (cargo never sees the
    # package) and is ambiguous with -ProgressAction on PowerShell 7.4+
    # (the call throws before cargo starts). $args forwards every token.
    if ($args.Count -lt 1) { throw "Invoke-Checked requires a command" }
    $file = [string]$args[0]
    $commandArgs = [string[]]@($args | Select-Object -Skip 1)
    & $file @commandArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$file $($commandArgs -join ' ') failed with exit $LASTEXITCODE"
    }
}

# The whole run, native tool output included (Invoke-Checked and
# Invoke-CargoBinBuild send it through the host), is saved to
# target/logs/build-<HEAD sha>-<timestamp>.log for the stamp trail. The
# transcript stops in finally, so a failed build leaves its log too.
$buildLog = Get-BuildLogPath -Root $root
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $buildLog) | Out-Null
Start-Transcript -LiteralPath $buildLog -Force | Out-Null
Write-Output "BUILD_LOG $buildLog"
try {
    Write-Step "build-tauri-windows.ps1 -Mode $Mode (PowerShell $($PSVersionTable.PSVersion))"
    if (-not (Test-IsWindowsHost)) { throw 'build-tauri-windows.ps1 builds Windows installers; run it on Windows.' }
    foreach ($tool in 'cargo', 'rustc', 'npm', 'npx', 'node') {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is required on PATH" }
    }
    Assert-NodeMajor -Major 26
    # Check the MSVC tools before any npm or cargo work so a machine without
    # them fails in seconds with the install command, not at the first link.
    if (-not (Test-MsvcBuildTool)) {
        throw ("Visual Studio 2022 Build Tools with the C++ workload are required to link the Tauri app and the MCP sidecar. " +
            "From an elevated prompt run: $(Get-MsvcBuildToolsInstallCommand)")
    }
    Import-VsDevEnv

    # Preflight: every file tauri.conf.json points at must exist before cargo
    # runs. A missing icon used to surface as a tauri-build panic in build.rs.
    $conf = Get-Content -LiteralPath (Join-Path $srcTauri 'tauri.conf.json') -Raw | ConvertFrom-Json
    foreach ($icon in @($conf.bundle.icon)) {
        if (-not (Test-Path -LiteralPath (Join-Path $srcTauri $icon))) { throw "tauri.conf.json bundle.icon $icon is missing under $srcTauri" }
    }
    $hooks = $conf.bundle.windows.nsis.installerHooks
    if ($hooks -and -not (Test-Path -LiteralPath (Join-Path $srcTauri $hooks))) { throw "NSIS installerHooks $hooks is missing" }
    if ($conf.build.beforeBuildCommand -ne 'npm run build') {
        throw "tauri.conf.json build.beforeBuildCommand must be 'npm run build' (it runs in apps/desktop); found '$($conf.build.beforeBuildCommand)'"
    }

    # The release embeds apps/desktop/public/library through frontendDist, and
    # *.wav is gitignored: a fresh worktree has none. SMAX printed BUILD_OK with
    # zero WAVs and the installed app failed with 'NotSupportedError: no
    # supported sources'. Every WAV in media.lock.json must be staged with the
    # locked bytes, or the build stops here (no skip).
    $wavProblems = @(Get-LibraryWavProblem -Root $root)
    if ($wavProblems.Count -gt 0) {
        $wavProblems | ForEach-Object { Write-Output $_ }
        throw "$($wavProblems.Count) library WAV(s) missing or not the media.lock.json bytes; stage them in apps\desktop\public\library before a release build"
    }
    Write-Step 'library WAVs match media.lock.json (sha256, bytes)'

    $triple = ((& rustc -vV) | Select-String -Pattern '^host: ').ToString().Split(':', 2)[1].Trim()
    if ($LASTEXITCODE -ne 0 -or -not $triple) { throw 'could not read the rustc host triple' }
    Write-Step "host triple $triple"

    # Sidecar freshness is cargo's fingerprint verdict plus a content hash, never
    # the file mtime: a no-op rebuild does not rewrite gen-audio-mcp.exe, so the
    # old mtime check called an up-to-date binary stale (SMAX had to run a scoped
    # clean of the two crates to get past it).
    Write-Step 'build gen-audio-mcp sidecar'
    $artifact = Invoke-CargoBinBuild -Package gen-audio-mcp -Bin gen-audio-mcp -ExtraArgs @('--release')
    $sidecar = Get-Item -LiteralPath $artifact.Executable
    $sidecarHash = Get-Sha256 -LiteralPath $sidecar.FullName
    Write-Step "sidecar $($sidecar.FullName) $($sidecar.Length) bytes sha256 $sidecarHash; cargo: $(if ($artifact.Fresh) { 'up to date (fingerprint fresh)' } else { 'rebuilt' })"

    # Handshake on a free loopback port. 8765 may belong to an installed app.
    $port = Get-FreeLoopbackPort
    $addr = "127.0.0.1:$port"
    Write-Step "mcp initialize handshake on $addr"
    $proc = Start-Process -FilePath $sidecar.FullName -ArgumentList @('--http', $addr) -PassThru -WindowStyle Hidden
    try {
        $healthy = $false
        foreach ($attempt in 1..40) {
            try {
                $health = Invoke-WebRequest -Uri "http://$addr/health" -UseBasicParsing -TimeoutSec 2 |
                    Select-Object -ExpandProperty Content | ConvertFrom-Json
                if ($health.service -ne 'gen-audio-mcp' -or $health.role -ne 'mcp' -or -not $health.stateless) {
                    throw 'HTTP listener answered /health but is not the Gen-Audio stateless MCP service'
                }
                if ([int]$health.pid -ne $proc.Id) {
                    throw "MCP health PID $($health.pid) does not match launched sidecar $($proc.Id)"
                }
                $healthy = $true
                break
            } catch {
                if ($proc.HasExited) { throw "gen-audio-mcp exited with $($proc.ExitCode) before /health answered" }
                Start-Sleep -Milliseconds 250
            }
        }
        if (-not $healthy) { throw "gen-audio-mcp did not answer http://$addr/health" }
        $body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"build-tauri-windows","version":"0.1.0"}}}'
        $response = Invoke-WebRequest -Uri "http://$addr/mcp" -Method POST -ContentType 'application/json' -Body $body -UseBasicParsing
        $init = $response.Content | ConvertFrom-Json
        if ($init.result.protocolVersion -ne '2025-03-26') { throw "initialize negotiated '$($init.result.protocolVersion)', expected 2025-03-26" }
        if ($init.result.serverInfo.name -ne 'gen-audio') { throw "initialize serverInfo.name was '$($init.result.serverInfo.name)', expected gen-audio" }
        Write-Step 'mcp handshake ok (2025-03-26, gen-audio)'
    } finally {
        if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction Stop }
        $descriptor = $runtimeConfig.mcpAddressFile
        if (Test-Path -LiteralPath $descriptor) {
            $lines = [IO.File]::ReadAllLines($descriptor)
            if ($lines.Count -ge 2 -and $lines[1] -eq "$($proc.Id)") {
                Remove-Item -LiteralPath $descriptor -Force -ErrorAction Stop
            }
        }
    }

    Write-Step 'build gen-audio-acp agent'
    $acpArtifact = Invoke-CargoBinBuild -Package gen-audio-acp -Bin gen-audio-acp -ExtraArgs @('--release')
    $acp = Get-Item -LiteralPath $acpArtifact.Executable
    $acpHash = Get-Sha256 -LiteralPath $acp.FullName
    Write-Step "acp $($acp.FullName) $($acp.Length) bytes sha256 $acpHash; cargo: $(if ($acpArtifact.Fresh) { 'up to date (fingerprint fresh)' } else { 'rebuilt' })"

    # ACP uses stdio JSON-RPC, not MCP's HTTP listener. Prove the built binary
    # negotiates protocol 1 before allowing Tauri to package it as externalBin.
    $psi = New-Object System.Diagnostics.ProcessStartInfo $acp.FullName
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.WorkingDirectory = $root
    $acpProc = [System.Diagnostics.Process]::Start($psi)
    try {
        $acpProc.StandardInput.WriteLine('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}')
        $acpProc.StandardInput.Flush()
        $line = $acpProc.StandardOutput.ReadLineAsync()
        if (-not $line.Wait(15000) -or [string]::IsNullOrWhiteSpace($line.Result)) {
            throw 'gen-audio-acp did not answer initialize within 15 s'
        }
        $acpInit = $line.Result | ConvertFrom-Json
        if ($acpInit.result.protocolVersion -ne 1) {
            throw "gen-audio-acp negotiated protocolVersion '$($acpInit.result.protocolVersion)', expected 1"
        }
        if ($acpInit.result.agentInfo.name -ne 'gen-audio') {
            throw "gen-audio-acp agentInfo.name was '$($acpInit.result.agentInfo.name)', expected gen-audio"
        }
        $acpProc.StandardInput.Close()
        if (-not $acpProc.WaitForExit(5000)) { throw 'gen-audio-acp did not exit when stdin closed' }
        if ($acpProc.ExitCode -ne 0) {
            throw "gen-audio-acp exited with $($acpProc.ExitCode): $($acpProc.StandardError.ReadToEnd())"
        }
    } finally {
        if ($acpProc -and -not $acpProc.HasExited) { Stop-Process -Id $acpProc.Id -Force -ErrorAction Stop }
        if ($acpProc) { $acpProc.Dispose() }
    }
    Write-Step 'acp handshake ok (protocol 1, gen-audio)'

    # Stage the sidecar where tauri externalBin expects it. Never copy a file
    # onto itself (the old script copied target\release\gen-audio-mcp.exe onto
    # the same path and failed).
    $binDir = Join-Path $srcTauri 'binaries'
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $staged = Join-Path $binDir "gen-audio-mcp-$triple.exe"
    if ([System.IO.Path]::GetFullPath($staged) -eq [System.IO.Path]::GetFullPath($sidecar.FullName)) { throw "refusing to copy $staged onto itself" }
    Copy-Item -LiteralPath $sidecar.FullName -Destination $staged -Force
    $stagedHash = Get-Sha256 -LiteralPath $staged
    if ($stagedHash -ne $sidecarHash) { throw "staged sidecar $staged has sha256 $stagedHash, but cargo's binary has $sidecarHash" }
    Write-Step "staged sidecar $staged (sha256 $stagedHash, same bytes as cargo's binary)"
    $stagedAcp = Join-Path $binDir "gen-audio-acp-$triple.exe"
    if ([System.IO.Path]::GetFullPath($stagedAcp) -eq [System.IO.Path]::GetFullPath($acp.FullName)) {
        throw "refusing to copy $stagedAcp onto itself"
    }
    Copy-Item -LiteralPath $acp.FullName -Destination $stagedAcp -Force
    $stagedAcpHash = Get-Sha256 -LiteralPath $stagedAcp
    if ($stagedAcpHash -ne $acpHash) {
        throw "staged sidecar $stagedAcp has sha256 $stagedAcpHash, but cargo's binary has $acpHash"
    }
    Write-Step "staged sidecar $stagedAcp (sha256 $stagedAcpHash, same bytes as cargo's binary)"

    Write-Step 'frontend dependencies (npm ci)'
    Push-Location $desktop
    try {
        Invoke-Checked npm ci
    } finally {
        Pop-Location
    }

    # A release build never carries the dev fixture cards.
    if (Test-Path Env:VITE_GEN_AUDIO_FIXTURES) {
        Write-Step 'clearing VITE_GEN_AUDIO_FIXTURES for the release build'
        Remove-Item Env:VITE_GEN_AUDIO_FIXTURES
    }

    # Outputs are judged by content, never mtime: a no-op rebuild leaves an
    # up-to-date gen-audio.exe with its old mtime, which the old mtime check
    # called stale. Fingerprint them before the build, compare after.
    # Installers are this build's version (tauri.conf.json), not the newest file.
    $installerGlob = @{ nsis = "*_$($conf.version)_*-setup.exe"; msi = "*_$($conf.version)_*.msi" }
    $indexPath = Join-Path $desktop 'dist\index.html'
    $exePath = Join-Path $releaseDir 'gen-audio.exe'
    $before = @{ index = Get-OutputFingerprint -LiteralPath $indexPath; exe = Get-OutputFingerprint -LiteralPath $exePath }
    foreach ($kind in 'nsis', 'msi') {
        foreach ($old in @(Get-ChildItem -Path (Join-Path $releaseDir "bundle\$kind") -Filter $installerGlob[$kind] -ErrorAction SilentlyContinue)) {
            $before[$old.FullName] = Get-OutputFingerprint -LiteralPath $old.FullName
        }
    }

    # tauri build runs beforeBuildCommand (npm run build) in apps/desktop, so
    # dist is rebuilt before cargo compiles the WebView.
    $tauriArgs = @('--no', '--', $TauriCli, 'build')
    if ($Mode -eq 'Full') { $tauriArgs += @('--bundles', 'nsis,msi') } else { $tauriArgs += '--no-bundle' }
    Write-Step "npx $($tauriArgs -join ' ')"
    Push-Location $srcTauri
    try {
        Invoke-Checked npx @tauriArgs
    } finally {
        Pop-Location
    }

    # The exe embeds dist, so dist must hold this tree's public files byte for
    # byte, and an index.html with the header whose script and style assets exist.
    $index = Assert-BuildOutput -LiteralPath $indexPath -What 'apps/desktop/dist/index.html' -Before $before.index
    $drift = @(Get-DistCopyDrift -Source (Join-Path $desktop 'public') -Dist (Join-Path $desktop 'dist'))
    if ($drift.Count -gt 0) {
        $drift | ForEach-Object { Write-Output $_ }
        throw "dist does not hold this tree's apps/desktop/public ($($drift.Count) file(s)); the embedded UI would be stale"
    }
    $html = Get-Content -LiteralPath $index.Path -Raw
    foreach ($ref in @([regex]::Matches($html, '(?:src|href)="/(assets/[^"]+)"') | ForEach-Object { $_.Groups[1].Value })) {
        if (-not (Test-Path -LiteralPath (Join-Path (Join-Path $desktop 'dist') $ref) -PathType Leaf)) { throw "dist/index.html references /$ref, which is not in dist" }
    }
    foreach ($marker in 'brand-row', 'ga-header-toolbar') {
        if ($html -notmatch [regex]::Escape($marker)) { throw "dist/index.html has no '$marker'; the embedded UI is not the Gen-Audio header build" }
    }
    if ($html -match '/src/main\.ts') { throw 'dist/index.html references /src/main.ts; that is the Vite dev page, not a production build' }
    # Release ships the Library deck only: no dev fixture assets, no example deck chunk.
    $distAssets = Get-Content -Raw -LiteralPath (Join-Path $desktop 'dist\library\assets.json')
    foreach ($claim in 'fixture_tone', 'reference_only') {
        if ($distAssets -match [regex]::Escape("`"$claim`"")) { throw "dist/library/assets.json carries a dev fixture ($claim); release must not ship spec-fixture, cube-fixture or bench-ref" }
    }
    $exampleChunk = @(Get-ChildItem -LiteralPath (Join-Path $desktop 'dist\assets') -Filter 'viewport.example-*.js' -ErrorAction SilentlyContinue)
    if ($exampleChunk.Count -gt 0) { throw "dist has $($exampleChunk[0].Name); VITE_GEN_AUDIO_FIXTURES leaked into a release build" }
    Write-Step "frontend dist ok $($index.Path) sha256 $($index.Sha256) ($($index.State); public files match; no dev fixtures)"

    $exe = Assert-BuildOutput -LiteralPath $exePath -What 'target\release\gen-audio.exe' -Before $before.exe
    # Provenance: tauri copies cargo's gen-audio-desktop binary to
    # target\release\gen-audio.exe and bundles that file. Exactly one cargo
    # unit must hold a bin with the same sha256: that proves the exe is this
    # build's compile output, not a leftover copy, and picks the cargo layout
    # (Get-CargoUnit) the checks below read. cargo 1.100 writes no
    # .fingerprint dir, so the old .fingerprint-only gate found 0 and failed.
    # Units an older cargo left in the other layout are listed and ignored.
    $desktopUnits = @(Get-CargoUnit -ProfileDir $releaseDir -Package 'gen-audio-desktop')
    # Where the unit's binary is: current layout build\<pkg>\<hash>\out\
    # gen_audio_desktop.exe (cargo 1.100); legacy layout deps\gen_audio_desktop.exe
    # (cargo 1.90 on Windows: bins carry no hash suffix, so the pdb name stays
    # stable) or deps\gen_audio_desktop-<hash>.exe. tauri copies it to
    # gen-audio.exe and may leave its bundle-type marker set there, so a
    # match is byte-equal except those three letters (Compare-TauriBundleExe).
    $binMatches = @(foreach ($unit in $desktopUnits) {
            if (@($unit.FingerprintFiles | Where-Object { $_ -match '^bin-gen[-_]audio[-_]desktop$' }).Count -eq 0) { continue }
            $candidates = if ($unit.Layout -eq 'current') { @(Join-Path $unit.Out 'gen_audio_desktop.exe') } else { @((Join-Path $unit.Out "gen_audio_desktop-$($unit.Hash).exe"), (Join-Path $unit.Out 'gen_audio_desktop.exe')) }
            foreach ($binPath in $candidates) {
                if (-not (Test-Path -LiteralPath $binPath -PathType Leaf)) { continue }
                $same = (Get-Sha256 -LiteralPath $binPath) -eq $exe.Sha256
                if (-not $same) {
                    try { $null = Compare-TauriBundleExe -Built $binPath -Payload $exe.Path; $same = $true } catch { $same = $false }
                }
                if ($same) { [pscustomobject]@{ Unit = $unit; Path = $binPath }; break }
            }
        })
    if ($binMatches.Count -ne 1) {
        throw ("expected exactly one cargo gen-audio-desktop bin unit under $releaseDir whose binary matches gen-audio.exe (sha256 $($exe.Sha256), or equal but for the tauri bundle-type marker), " +
            "found $($binMatches.Count) ($($desktopUnits.Count) desktop unit(s) seen); the exe is not provably this build's")
    }
    $layout = $binMatches[0].Unit.Layout
    $units = @($desktopUnits | Where-Object { $_.Layout -eq $layout })
    $ignored = @($desktopUnits | Where-Object { $_.Layout -ne $layout })
    Write-Step "exe provenance: cargo unit $($binMatches[0].Unit.Hash) ($layout layout) $($binMatches[0].Path) matches gen-audio.exe$(if ($ignored.Count) { "; ignoring $($ignored.Count) unit(s) in the $($ignored[0].Layout) layout" })"

    # No localhost: a dev build (no custom-protocol) makes tauri-build emit
    # cargo:rustc-cfg=dev, and that exe loads devUrl http://localhost:1420.
    # Every gen-audio-desktop build-script output is checked, whatever its
    # mtime (a no-op rebuild does not rewrite it; the fingerprint gate below
    # allows only one desktop configuration). None found is a failure: a
    # check over zero outputs proves nothing.
    $outputs = @($units | Where-Object { Test-Path -LiteralPath $_.BuildScriptOutput -PathType Leaf } |
            ForEach-Object { Get-Item -LiteralPath $_.BuildScriptOutput })
    if ($outputs.Count -eq 0) { throw "no gen-audio-desktop build-script output in cargo's $layout layout under $releaseDir; cannot rule out a dev (devUrl) build" }
    foreach ($output in $outputs) {
        if (Select-String -LiteralPath $output.FullName -Pattern 'cargo:rustc-cfg=dev' -SimpleMatch -Quiet) {
            throw "dev build detected ($($output.FullName) has cargo:rustc-cfg=dev); this exe would load http://localhost:1420"
        }
    }
    Write-Step "no dev cfg in gen-audio-desktop build-script output ($($outputs.Count) checked)"
    # Fingerprint gate: one desktop lib fingerprint in the layout cargo used.
    # More than one means the desktop crate was built under a second
    # configuration (bare cargo, another CLI, other features) and the cache
    # is thrashing.
    $fingerprints = @($units | Where-Object { @($_.FingerprintFiles | Where-Object { $_ -like 'lib-gen_audio_desktop_lib*' }).Count -gt 0 })
    Write-Step "gen-audio-desktop lib fingerprints: $($fingerprints.Count) ($layout layout: $(($fingerprints | ForEach-Object { $_.Hash }) -join ', '))"
    if ($fingerprints.Count -ne 1) {
        throw "expected exactly one gen-audio-desktop lib fingerprint in cargo's $layout layout under $releaseDir, found $($fingerprints.Count)"
    }
    Write-Step "exe $($exe.Path) sha256 $($exe.Sha256) ($($exe.State))"

    if ($Mode -eq 'Full') {
        $sevenZip = Get-SevenZip
        if (-not $sevenZip) { throw '7-Zip (7z.exe) is required to check the exe inside the NSIS and MSI installers against the built exe' }
        $bundleDir = Join-Path $releaseDir 'bundle'
        foreach ($kind in 'nsis', 'msi') {
            $found = @(Get-ChildItem -Path (Join-Path $bundleDir $kind) -Filter $installerGlob[$kind] -ErrorAction SilentlyContinue)
            if ($found.Count -ne 1) { throw "expected one $kind installer $($installerGlob[$kind]) (version $($conf.version)) under $bundleDir\$kind, found $($found.Count): $(($found | ForEach-Object { $_.Name }) -join ', ')" }
            $installer = $found[0].FullName
            $built = Assert-BuildOutput -LiteralPath $installer -What "$kind installer" -Before $before[$installer]
            Write-Output "INSTALLER path=$($built.Path) size=$($built.Length) sha256=$($built.Sha256) state=$($built.State)"
            # The exe the installer actually carries (NSIS: gen-audio.exe; MSI cab: Path).
            $payloadDir = Join-Path $releaseDir "payload-check\$kind"
            if (Test-Path -LiteralPath $payloadDir) { Remove-Item -LiteralPath $payloadDir -Recurse -Force }
            New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null
            try {
                $member = if ($kind -eq 'nsis') { 'gen-audio.exe' } else { 'Path' }
                Invoke-Checked $sevenZip e $installer $member "-o$payloadDir" -y | Out-Null
                $payloadExe = Join-Path $payloadDir $member
                if (-not (Test-Path -LiteralPath $payloadExe -PathType Leaf)) { throw "$kind installer has no $member payload" }
                $match = Compare-TauriBundleExe -Built $exe.Path -Payload $payloadExe
                Write-Output "INSTALLER_PAYLOAD kind=$kind sha256=$($match.PayloadSha256) bundle_type=$($match.PayloadType) built_bundle_type=$($match.BuiltType) marker_offset=$($match.Offset) other_bytes=identical"
            } finally {
                Remove-Item -LiteralPath (Join-Path $releaseDir 'payload-check') -Recurse -Force -ErrorAction SilentlyContinue
            }
        }

        # Run only after the NSIS and MSI outputs (including the app payload)
        # have passed their content/provenance gates. Pass those exact hashes so
        # MSIX and NuGet cannot silently package different release binaries.
        $packArgs = @{
            ExpectedExeSha256 = $exe.Sha256
            ExpectedMcpSha256 = $sidecarHash
            ExpectedAcpSha256 = $acpHash
        }
        if ($CertificateThumbprint) { $packArgs.CertificateThumbprint = $CertificateThumbprint }
        & (Join-Path $PSScriptRoot 'pack-windows-msix-nuget.ps1') @packArgs
    }

    Write-Output "BUILD_OK mode=$Mode exe=$($exe.Path) size=$($exe.Length) sha256=$($exe.Sha256) state=$($exe.State) log=$buildLog"
} catch {
    # Record the failure inside the transcript before it closes, then rethrow.
    Write-Output "BUILD_FAIL log=$buildLog $($_.Exception.Message)"
    throw
} finally {
    Stop-Transcript | Out-Null
}
