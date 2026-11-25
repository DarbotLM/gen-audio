BeforeAll {
    $script:RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $script:DevEnv = Join-Path $script:RepoRoot 'scripts\lib\devenv.ps1'
    $script:BuildScript = Join-Path $script:RepoRoot 'scripts\build-tauri-windows.ps1'
    $script:Packer = Join-Path $script:RepoRoot 'scripts\pack-windows-msix-nuget.ps1'
    . $script:DevEnv

    function New-LibraryFixture {
        param(
            [string]$Root,
            [hashtable]$Files,
            [hashtable]$Locked
        )
        $library = Join-Path $Root 'apps\desktop\public\library'
        New-Item -ItemType Directory -Force -Path $library, (Join-Path $Root 'schemas\asset-object') | Out-Null
        foreach ($name in $Files.Keys) {
            [IO.File]::WriteAllBytes((Join-Path $library $name), $Files[$name])
        }
        $media = @{}
        foreach ($name in $Locked.Keys) {
            $bytes = [byte[]]$Locked[$name]
            $media[$name] = @{
                bytes = $bytes.Length
                sha256 = (Get-FileHash -InputStream ([IO.MemoryStream]::new($bytes)) -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
        @{ media = $media } | ConvertTo-Json -Depth 5 |
            Set-Content -LiteralPath (Join-Path $Root 'schemas\asset-object\media.lock.json') -Encoding UTF8
        return $Root
    }

    function Invoke-Captured {
        param(
            [string]$File,
            [string[]]$Arguments,
            [string]$WorkingDirectory
        )
        Push-Location $WorkingDirectory
        try {
            $output = @(& $File @Arguments 2>&1 | ForEach-Object { "$_" })
            return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output -join [Environment]::NewLine) }
        } finally {
            Pop-Location
        }
    }
}

Describe 'library WAV build gate' {
    It 'names missing, changed, and wrong-sized WAVs' {
        $good = [Text.Encoding]::UTF8.GetBytes('RIFF good')
        $other = [Text.Encoding]::UTF8.GetBytes('RIFF other')
        $root = New-LibraryFixture -Root (Join-Path $TestDrive 'root') -Files @{
            'a.wav' = $good
            'c.wav' = [Text.Encoding]::UTF8.GetBytes('RIFF goox')
            'd.wav' = $good + [byte]120
        } -Locked @{
            'a.wav' = $good
            'b.wav' = $other
            'c.wav' = $good
            'd.wav' = $good
        }
        $problems = @(Get-LibraryWavProblem -Root $root)
        ($problems | Where-Object { $_ -like 'WAV_MISSING b.wav*' }) | Should -Not -BeNullOrEmpty
        ($problems | Where-Object { $_ -like 'WAV_MISMATCH c.wav*' }) | Should -Not -BeNullOrEmpty
        ($problems | Where-Object { $_ -like 'WAV_BYTES d.wav*' }) | Should -Not -BeNullOrEmpty
        ($problems | Where-Object { $_ -like '*a.wav*' }) | Should -BeNullOrEmpty

        $clean = New-LibraryFixture -Root (Join-Path $TestDrive 'clean') -Files @{ 'a.wav' = $good } -Locked @{ 'a.wav' = $good }
        @(Get-LibraryWavProblem -Root $clean) | Should -BeNullOrEmpty
    }

    It 'fails closed for an empty or missing WAV lock' {
        $empty = New-LibraryFixture -Root (Join-Path $TestDrive 'empty') -Files @{} -Locked @{}
        { Get-LibraryWavProblem -Root $empty } | Should -Throw
        $missing = Join-Path $TestDrive 'missing'
        New-Item -ItemType Directory -Path $missing | Out-Null
        { Get-LibraryWavProblem -Root $missing } | Should -Throw
    }

    It 'stages only WAVs whose bytes match the lock' {
        $good = [Text.Encoding]::UTF8.GetBytes('RIFF good')
        $other = [Text.Encoding]::UTF8.GetBytes('RIFF other')
        $root = New-LibraryFixture -Root (Join-Path $TestDrive 'import') -Files @{} -Locked @{
            'a.wav' = $good
            'b.wav' = $other
            'c.wav' = $good
        }
        $source = Join-Path $TestDrive 'wav-source'
        New-Item -ItemType Directory -Path $source | Out-Null
        [IO.File]::WriteAllBytes((Join-Path $source 'a.wav'), $good)
        [IO.File]::WriteAllBytes((Join-Path $source 'b.wav'), $other + [byte]120)
        $result = @(Import-LibraryWav -Root $root -From $source)
        ($result -join "`n") | Should -Match 'WAV_STAGED a\.wav'
        ($result -join "`n") | Should -Match 'WAV_SOURCE_MISMATCH b.wav'
        ($result -join "`n") | Should -Match 'WAV_SOURCE_MISSING c.wav'
        [IO.File]::ReadAllBytes((Join-Path $root 'apps\desktop\public\library\a.wav')) | Should -Be $good
        (Join-Path $root 'apps\desktop\public\library\b.wav') | Should -Not -Exist
        (Join-Path $root 'apps\desktop\public\library\c.wav') | Should -Not -Exist
        @(Get-LibraryWavProblem -Root $root) | Should -HaveCount 2
        { Import-LibraryWav -Root $root -From (Join-Path $TestDrive 'missing-source') } | Should -Throw
    }

    It 'checks WAVs before either native build' {
        $text = Get-Content -LiteralPath $script:BuildScript -Raw
        $text | Should -Match '\[string\]\$WavSource'
        $gate = $text.IndexOf('Get-LibraryWavProblem -Root $root')
        $gate | Should -BeGreaterThan -1
        $text.IndexOf('Import-LibraryWav -Root $root -From $WavSource') | Should -BeLessThan $gate
        $gate | Should -BeLessThan $text.IndexOf('Invoke-CargoBinBuild -Package')
        $gate | Should -BeLessThan $text.IndexOf('npx $($tauriArgs')
        $block = $text.Substring($gate, $text.IndexOf("Write-Step 'library WAVs match", $gate) - $gate)
        $block | Should -Match 'throw'
        $block | Should -Match '-WavSource'
        $block.ToLowerInvariant() | Should -Not -Match '\bskip\b'
    }
}

Describe 'native cargo build freshness' {
    It 'selects the requested cargo binary artifact' {
        $lines = @(
            @{ reason = 'compiler-artifact'; target = @{ name = 'gen_audio_core'; kind = @('lib') }; executable = $null; fresh = $true }
            @{ reason = 'compiler-artifact'; target = @{ name = 'gen-audio-mcp'; kind = @('bin') }; executable = 'C:/t/release/gen-audio-mcp.exe'; fresh = $true }
            @{ reason = 'build-finished'; success = $true }
        ) | ForEach-Object { $_ | ConvertTo-Json -Compress }
        $artifact = Select-CargoBinArtifact -Line $lines -Bin gen-audio-mcp
        $artifact.Executable | Should -Be 'C:/t/release/gen-audio-mcp.exe'
        $artifact.Fresh | Should -BeTrue
        { Select-CargoBinArtifact -Line $lines -Bin other } | Should -Throw '*no compiler-artifact for bin other*'
    }

    It 'judges sidecars by cargo freshness and hash, not time' {
        $text = Get-Content -LiteralPath $script:BuildScript -Raw
        $block = $text.Substring($text.IndexOf("Write-Step 'build gen-audio-mcp sidecar'"), $text.IndexOf("Write-Step 'frontend dependencies") - $text.IndexOf("Write-Step 'build gen-audio-mcp sidecar'"))
        $block | Should -Match 'Invoke-CargoBinBuild -Package gen-audio-mcp -Bin gen-audio-mcp'
        $block | Should -Match '\$stagedHash -ne \$sidecarHash'
        $block | Should -Not -Match 'Assert-Fresh'
        $block | Should -Not -Match 'LastWriteTime'
    }
}

Describe 'build and packaging contracts' {
    It 'routes cube layer generation through the F# native host' {
        $scriptText = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'scripts\cube_layers.ps1') -Raw
        $hostText = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'src\wingen.audio\Program.cs') -Raw
        $cubeText = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'src\WingenAudio.Contracts\CubeLayers.fs') -Raw
        $scriptText | Should -Match 'Resolve-WingenAudioNativeHost'
        $scriptText | Should -Match '''cube-layers'''
        $scriptText | Should -Not -Match '&\s*(python|pytest|cargo)\b'
        $hostText | Should -Match '"cube-layers" => RunCubeLayers'
        $hostText | Should -Match 'CubeLayers\.Generate'
        $cubeText | Should -Match 'let private stft'
        $cubeText | Should -Match 'let private computeLayers'
        $cubeText | Should -Match 'let private writePng'
        $cubeText | Should -Match 'generatorPath = "src/WingenAudio.Contracts/CubeLayers.fs"'
    }

    It 'builds both locked sidecars and uses the local Tauri CLI' {
        $text = Get-Content -LiteralPath $script:BuildScript -Raw
        $text | Should -Match ([regex]::Escape('$TauriCli = ''tauri'''))
        $text | Should -Match ([regex]::Escape('$tauriArgs = @(''--no'', ''--'', $TauriCli, ''build'')'))
        $block = $text.Substring($text.IndexOf("Write-Step 'build gen-audio-mcp sidecar'"), $text.IndexOf("Write-Step 'frontend dependencies") - $text.IndexOf("Write-Step 'build gen-audio-mcp sidecar'"))
        $block | Should -Match 'Invoke-CargoBinBuild -Package gen-audio-mcp -Bin gen-audio-mcp'
        $block | Should -Match 'Invoke-CargoBinBuild -Package gen-audio-acp -Bin gen-audio-acp'
        $block | Should -Match 'acpInit\.result\.protocolVersion -ne 1'
        $block | Should -Match "acpInit\.result\.agentInfo\.name -ne 'gen-audio'"
        $block | Should -Match '\$stagedHash -ne \$sidecarHash'
        $block | Should -Match '\$stagedAcpHash -ne \$acpHash'
        $config = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'apps\desktop\src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json
        $config.bundle.externalBin | Should -Be @('binaries/gen-audio-mcp', 'binaries/gen-audio-acp', 'binaries/wingen.audio')
        $buildRs = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'apps\desktop\src-tauri\build.rs') -Raw
        $debug = $buildRs.Substring($buildRs.IndexOf('std::env::set_var'))
        $debug = $debug.Substring(0, $debug.IndexOf('} else {'))
        $debug | Should -Match '"bundle":\{"externalBin":\["binaries/gen-audio-mcp"\]\}'
    }

    It 'runs provenance checks before MSIX and NuGet packaging' {
        $build = Get-Content -LiteralPath $script:BuildScript -Raw
        $pack = Get-Content -LiteralPath $script:Packer -Raw
        $build.IndexOf('INSTALLER_PAYLOAD kind=$kind') | Should -BeLessThan $build.IndexOf('pack-windows-msix-nuget.ps1', $build.IndexOf('INSTALLER_PAYLOAD kind=$kind'))
        foreach ($expected in @('ExpectedExeSha256 = $exe.Sha256', 'ExpectedMcpSha256 = $sidecarHash', 'ExpectedAcpSha256 = $acpHash')) {
            $build | Should -Match ([regex]::Escape($expected))
        }
        $build | Should -Match ([regex]::Escape('ExpectedWingenSha256 = $wingenHash'))
        foreach ($name in @('gen-audio.exe', 'gen-audio-mcp.exe', 'gen-audio-acp.exe', 'wingen.audio.exe')) {
            $pack | Should -Match ([regex]::Escape($name))
        }
        $pack | Should -Match ([regex]::Escape("Assert-ArchivePayload -Kind 'msix'"))
        $pack | Should -Match ([regex]::Escape("Assert-ArchivePayload -Kind 'nupkg'"))
        $pack | Should -Match 'if \(\$certificate\)'
    }

    It 'fingerprints outputs by content rather than mtime' {
        $output = Join-Path $TestDrive 'gen-audio.exe'
        [IO.File]::WriteAllBytes($output, [Text.Encoding]::UTF8.GetBytes('MZ v1'))
        $before = Get-OutputFingerprint -LiteralPath $output
        [IO.File]::SetLastWriteTime($output, [datetime]'2001-01-01')
        $same = Assert-BuildOutput -LiteralPath $output -What exe -Before $before
        $same.State | Should -Be 'unchanged'
        $same.Sha256 | Should -Be (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash
        [IO.File]::WriteAllText($output, 'MZ v2')
        (Assert-BuildOutput -LiteralPath $output -What exe -Before $before).State | Should -Be 'rebuilt'
        $new = Join-Path $TestDrive 'new.msi'
        $snapshot = Get-OutputFingerprint -LiteralPath $new
        [IO.File]::WriteAllText($new, 'msi')
        (Assert-BuildOutput -LiteralPath $new -What installer -Before $snapshot).State | Should -Be 'rebuilt'
        { Assert-BuildOutput -LiteralPath (Join-Path $TestDrive 'gone.exe') -What exe -Before $null } |
            Should -Throw '*exe is missing after the build*'
    }

    It 'reports missing and changed desktop distribution files' {
        $public = Join-Path $TestDrive 'public'
        $dist = Join-Path $TestDrive 'dist'
        New-Item -ItemType Directory -Force -Path (Join-Path $public 'library'), (Join-Path $dist 'library') | Out-Null
        '{}' | Set-Content -LiteralPath (Join-Path $public 'library\assets.json') -Encoding UTF8
        '{}' | Set-Content -LiteralPath (Join-Path $dist 'library\assets.json') -Encoding UTF8
        @(Get-DistCopyDrift -Source $public -Dist $dist) | Should -BeNullOrEmpty
        '{"new": 1}' | Set-Content -LiteralPath (Join-Path $public 'library\assets.json') -Encoding UTF8
        [IO.File]::WriteAllBytes((Join-Path $public 'library\a.wav'), [byte[]](1, 2, 3))
        @(Get-DistCopyDrift -Source $public -Dist $dist) | Sort-Object |
            Should -Be @('DIST_CHANGED library/assets.json', 'DIST_MISSING library/a.wav')
    }

    It 'keeps desktop dependencies on pinned supported versions' {
        $package = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'apps\desktop\package.json') -Raw | ConvertFrom-Json
        $lockJson = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'apps\desktop\package-lock.json') -Raw
        $lock = if ($PSVersionTable.PSVersion.Major -ge 7) {
            $lockJson | ConvertFrom-Json -AsHashtable
        } else {
            Add-Type -AssemblyName System.Web.Extensions
            $serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
            $serializer.MaxJsonLength = 20MB
            $serializer.DeserializeObject($lockJson)
        }
        $package.engines.node | Should -Be '>=26.0.0 <27'
        $package.dependencies.'@tauri-apps/api' | Should -Be '2.12.5'
        $package.devDependencies.'@tauri-apps/cli' | Should -Be '2.12.6'
        $package.devDependencies.vite | Should -Be '^6.4.5'
        $packagesByName = if ($lock -is [Collections.IDictionary]) { $lock['packages'] } else { $lock.PSObject.Properties['packages'].Value }
        $vite = if ($packagesByName -is [Collections.IDictionary]) {
            $packagesByName['node_modules/vite']
        } else {
            $packagesByName.PSObject.Properties['node_modules/vite'].Value
        }
        $vite.version | Should -Be '6.4.5'
        $tauriApi = $packagesByName['node_modules/@tauri-apps/api']
        $tauriCli = $packagesByName['node_modules/@tauri-apps/cli']
        $tauriApi.version | Should -Be '2.12.5'
        $tauriCli.version | Should -Be '2.12.6'
        $packages = if ($packagesByName -is [Collections.IDictionary]) { $packagesByName.Values } else { $packagesByName.PSObject.Properties.Value }
        $resolved = @($packages | ForEach-Object {
            if ($_ -is [Collections.IDictionary]) {
                if ($_.ContainsKey('resolved')) { $_['resolved'] }
            } elseif ($_.PSObject.Properties['resolved']) {
                $_.resolved
            }
        })
        $resolved | Should -Not -BeNullOrEmpty
        $resolved | ForEach-Object { $_ | Should -Match '^https://registry\.npmjs\.org/' }
        (Get-Content -LiteralPath (Join-Path $script:RepoRoot 'apps\desktop\.npmrc') -Raw) |
            Should -Match 'registry=https://registry\.npmjs\.org/'
    }

    It 'keeps every release version and runtime announcement synchronized' {
        $expectedMatch = [regex]::Match(
            (Get-Content -LiteralPath (Join-Path $script:RepoRoot 'Cargo.toml') -Raw),
            '(?ms)^\[workspace\.package\]\s*$.*?^version\s*=\s*"([^"]+)"'
        )
        $expectedMatch.Success | Should -BeTrue
        $expected = $expectedMatch.Groups[1].Value

        $versions = @{
            'pyproject.toml' = (Get-Content -LiteralPath (Join-Path $script:RepoRoot 'pyproject.toml') -Raw)
            'src/gen_audio/__init__.py' = (Get-Content -LiteralPath (Join-Path $script:RepoRoot 'src\gen_audio\__init__.py') -Raw)
            'uv.lock' = (Get-Content -LiteralPath (Join-Path $script:RepoRoot 'uv.lock') -Raw)
        }
        $package = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'apps\desktop\package.json') -Raw | ConvertFrom-Json
        $packageLockJson = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'apps\desktop\package-lock.json') -Raw
        $packageLock = if ($PSVersionTable.PSVersion.Major -ge 7) {
            $packageLockJson | ConvertFrom-Json -AsHashtable
        } else {
            Add-Type -AssemblyName System.Web.Extensions
            $serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
            $serializer.MaxJsonLength = 20MB
            $serializer.DeserializeObject($packageLockJson)
        }
        $tauri = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'apps\desktop\src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json
        $versions['pyproject.toml'] | Should -Match "(?m)^version\s*=\s*`"$([regex]::Escape($expected))`""
        $versions['src/gen_audio/__init__.py'] | Should -Match "__version__\s*=\s*`"$([regex]::Escape($expected))`""
        $uvPackage = [regex]::Match(
            $versions['uv.lock'],
            '(?ms)^name = "gen-audio"\s*$.*?^version = "([^"]+)"'
        )
        $uvPackage.Success | Should -BeTrue
        $uvPackage.Groups[1].Value | Should -Be $expected
        $package.version | Should -Be $expected
        $packageLock['version'] | Should -Be $expected
        $packageLock['packages'][''].version | Should -Be $expected
        $tauri.version | Should -Be $expected

        foreach ($contract in @(
            @{ Path = 'crates\gen-audio-mcp\src\lib.rs'; Key = 'serverInfo' },
            @{ Path = 'crates\gen-audio-acp\src\lib.rs'; Key = 'agentInfo' }
        )) {
            $lines = Get-Content -LiteralPath (Join-Path $script:RepoRoot $contract.Path)
            $line = $lines | Where-Object { $_ -match "`"$($contract.Key)`"" } | Select-Object -First 1
            $line | Should -Match 'env!\("CARGO_PKG_VERSION"\)'
        }
    }

    It 'stores build transcripts and closes them in finally' {
        $text = Get-Content -LiteralPath $script:BuildScript -Raw
        $text | Should -Match 'Write-Output "BUILD_OK [^"]*log=\$buildLog'
        $start = $text.IndexOf('Start-Transcript')
        $body = $text.Substring($start)
        $body | Should -Match '\}\s*finally\s*\{[\s\S]*?Stop-Transcript'
        $text.IndexOf('Write-Step "build-tauri-windows.ps1 -Mode') | Should -BeGreaterThan $start
        foreach ($old in @('Assert-Fresh', 'LastWriteTime', '$runStart')) {
            $text | Should -Not -Match ([regex]::Escape($old))
        }
        $text | Should -Match '\$indexPath = Join-Path \$desktop ''dist\\index\.html'''
        $text | Should -Match '\$exePath = Join-Path \$releaseDir ''gen-audio\.exe'''
        foreach ($variable in @('$indexPath', '$exePath', '$installer')) {
            $text | Should -Match ([regex]::Escape("Assert-BuildOutput -LiteralPath $variable "))
        }
        $text | Should -Match 'Get-DistCopyDrift -Source'
        $text | Should -Match '\$conf\.version'
    }

    It 'writes and closes a transcript when a build fails' -Skip:(($env:OS -eq 'Windows_NT') -or -not (Get-Command git -ErrorAction SilentlyContinue)) {
        $root = Join-Path $TestDrive 'build-repo'
        New-Item -ItemType Directory -Force -Path (Join-Path $root 'scripts\lib') | Out-Null
        foreach ($relative in @('scripts\build-tauri-windows.ps1', 'scripts\lib\devenv.ps1', 'scripts\runtime.json', 'scripts\lib\runtime-config.ps1')) {
            $destination = Join-Path $root $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
            Copy-Item -LiteralPath (Join-Path $script:RepoRoot $relative) -Destination $destination
        }
        $git = @('git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid')
        (Invoke-Captured -File $git[0] -Arguments (@($git | Select-Object -Skip 1) + @('init', '-q')) -WorkingDirectory $root).ExitCode | Should -Be 0
        (Invoke-Captured -File 'git' -Arguments (@('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'add', '.')) -WorkingDirectory $root).ExitCode | Should -Be 0
        (Invoke-Captured -File 'git' -Arguments (@('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-m', 'seed')) -WorkingDirectory $root).ExitCode | Should -Be 0
        $sha = (Invoke-Captured -File 'git' -Arguments @('rev-parse', '--short=12', 'HEAD') -WorkingDirectory $root).Output
        $result = Invoke-Captured -File (Get-Process -Id $PID).Path `
            -Arguments @('-NoProfile', '-File', (Join-Path $root 'scripts\build-tauri-windows.ps1'), '-Mode', 'NoBundle') `
            -WorkingDirectory $root
        $result.ExitCode | Should -Not -Be 0
        $result.Output | Should -Match 'run it on Windows'
        $logs = @(Get-ChildItem -LiteralPath (Join-Path $root 'target\logs') -Filter "build-$sha-*.log")
        $logs | Should -HaveCount 1
        $logs[0].Name | Should -Match "^build-$sha-\d{8}-\d{6}\.log$"
        $transcript = Get-Content -LiteralPath $logs[0].FullName -Raw
        $transcript | Should -Match 'build-tauri-windows\.ps1 -Mode NoBundle'
        $transcript | Should -Match 'run it on Windows'
        $transcript | Should -Match 'PowerShell transcript end'
        $result.Output | Should -Match ([regex]::Escape("BUILD_LOG $($logs[0].FullName)"))
    }
}
