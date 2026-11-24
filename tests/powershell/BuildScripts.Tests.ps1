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

    function New-CargoProbe {
        param([string]$Root)
        New-Item -ItemType Directory -Force -Path (Join-Path $Root 'src') | Out-Null
        @'
[package]
name = "probe"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "probe"
path = "src/main.rs"

[workspace]
'@ | Set-Content -LiteralPath (Join-Path $Root 'Cargo.toml') -Encoding UTF8
        'fn main() { println!("v1"); }' | Set-Content -LiteralPath (Join-Path $Root 'src\main.rs') -Encoding UTF8
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

    It 'checks WAVs before either native build' {
        $text = Get-Content -LiteralPath $script:BuildScript -Raw
        $gate = $text.IndexOf('Get-LibraryWavProblem -Root $root')
        $gate | Should -BeGreaterThan -1
        $gate | Should -BeLessThan $text.IndexOf('Invoke-CargoBinBuild -Package')
        $gate | Should -BeLessThan $text.IndexOf('npx $($tauriArgs')
        $block = $text.Substring($gate, $text.IndexOf("Write-Step 'library WAVs match", $gate) - $gate)
        $block | Should -Match 'throw'
        $block.ToLowerInvariant() | Should -Not -Match '\bskip\b'
    }
}

Describe 'native cargo build freshness' {
    It 'treats a no-op rebuild as fresh regardless of executable mtime' -Skip:(-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
        $crate = Join-Path $TestDrive 'probe'
        New-CargoProbe -Root $crate
        Push-Location $crate
        try {
            $saved = $env:CARGO_TARGET_DIR
            $env:CARGO_TARGET_DIR = Join-Path $crate 'target'
            $first = Invoke-CargoBinBuild -Package probe -Bin probe
            $first.Fresh | Should -BeFalse
            $first.Executable | Should -Exist
            $before = (Get-FileHash -LiteralPath $first.Executable -Algorithm SHA256).Hash
            [IO.File]::SetLastWriteTime($first.Executable, [datetime]'2001-01-01')
            $second = Invoke-CargoBinBuild -Package probe -Bin probe
            $second.Fresh | Should -BeTrue
            $second.Executable | Should -Be $first.Executable
            (Get-FileHash -LiteralPath $first.Executable -Algorithm SHA256).Hash | Should -Be $before
            'fn main() { println!("v2"); }' | Set-Content -LiteralPath (Join-Path $crate 'src\main.rs') -Encoding UTF8
            (Invoke-CargoBinBuild -Package probe -Bin probe).Fresh | Should -BeFalse
            (Get-FileHash -LiteralPath $first.Executable -Algorithm SHA256).Hash | Should -Not -Be $before
        } finally {
            $env:CARGO_TARGET_DIR = $saved
            Pop-Location
        }
    }

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
    It 'builds both locked sidecars and uses the local Tauri CLI' {
        $text = Get-Content -LiteralPath $script:BuildScript -Raw
        $text | Should -Match ([regex]::Escape('$TauriCli = ''tauri'''))
        $block = $text.Substring($text.IndexOf("Write-Step 'build gen-audio-mcp sidecar'"), $text.IndexOf("Write-Step 'frontend dependencies") - $text.IndexOf("Write-Step 'build gen-audio-mcp sidecar'"))
        $block | Should -Match 'Invoke-CargoBinBuild -Package gen-audio-mcp -Bin gen-audio-mcp'
        $block | Should -Match 'Invoke-CargoBinBuild -Package gen-audio-acp -Bin gen-audio-acp'
        $block | Should -Match '\$stagedHash -ne \$sidecarHash'
        $block | Should -Match '\$stagedAcpHash -ne \$acpHash'
        $config = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'apps\desktop\src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json
        $config.bundle.externalBin | Should -Be @('binaries/gen-audio-mcp', 'binaries/gen-audio-acp')
    }

    It 'runs provenance checks before MSIX and NuGet packaging' {
        $build = Get-Content -LiteralPath $script:BuildScript -Raw
        $pack = Get-Content -LiteralPath $script:Packer -Raw
        $build.IndexOf('INSTALLER_PAYLOAD kind=$kind') | Should -BeLessThan $build.IndexOf('pack-windows-msix-nuget.ps1', $build.IndexOf('INSTALLER_PAYLOAD kind=$kind'))
        foreach ($expected in @('ExpectedExeSha256 = $exe.Sha256', 'ExpectedMcpSha256 = $sidecarHash', 'ExpectedAcpSha256 = $acpHash')) {
            $build | Should -Match ([regex]::Escape($expected))
        }
        foreach ($name in @('gen-audio.exe', 'gen-audio-mcp.exe', 'gen-audio-acp.exe')) {
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
        $package.devDependencies.vite | Should -Be '^6.4.5'
        $packagesByName = if ($lock -is [Collections.IDictionary]) { $lock['packages'] } else { $lock.PSObject.Properties['packages'].Value }
        $vite = if ($packagesByName -is [Collections.IDictionary]) {
            $packagesByName['node_modules/vite']
        } else {
            $packagesByName.PSObject.Properties['node_modules/vite'].Value
        }
        $vite.version | Should -Be '6.4.5'
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
    }

    It 'stores build transcripts and closes them in finally' {
        $text = Get-Content -LiteralPath $script:BuildScript -Raw
        $text | Should -Match 'Write-Output "BUILD_OK [^"]*log=\$buildLog'
        $start = $text.IndexOf('Start-Transcript')
        $body = $text.Substring($start)
        $body | Should -Match '\}\s*finally\s*\{\s*Stop-Transcript'
        $text.IndexOf('Write-Step "build-tauri-windows.ps1 -Mode') | Should -BeGreaterThan $start
        foreach ($old in @('Assert-Fresh', 'LastWriteTime', '$runStart')) {
            $text | Should -Not -Match ([regex]::Escape($old))
        }
    }
}
