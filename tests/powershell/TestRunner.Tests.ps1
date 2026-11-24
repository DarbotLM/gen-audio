BeforeAll {
    $script:RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $script:Shell = (Get-Process -Id $PID).Path
    $script:SkippedSteps = 'Pssa,Sprawl,Pester,Regen,Cargo,Npm,Python'

    function Invoke-Captured {
        param(
            [string]$File,
            [string[]]$Arguments,
            [string]$WorkingDirectory
        )
        Push-Location $WorkingDirectory
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& $File @Arguments 2>&1 | ForEach-Object { "$_" })
            return [pscustomobject]@{
                ExitCode = $LASTEXITCODE
                Output = ($output -join [Environment]::NewLine)
            }
        } finally {
            $ErrorActionPreference = $previousErrorAction
            Pop-Location
        }
    }

    function Invoke-Git {
        param([string]$Root, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
        $result = Invoke-Captured -File 'git' -Arguments (@('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'core.autocrlf=false') + $Arguments) -WorkingDirectory $Root
        if ($result.ExitCode -ne 0) { throw $result.Output }
        return $result.Output
    }

    function New-TestRepository {
        param([string]$Path)
        New-Item -ItemType Directory -Force -Path (Join-Path $Path 'scripts\lib') | Out-Null
        foreach ($relative in @('scripts\test.ps1', 'scripts\lib\devenv.ps1')) {
            $source = Join-Path $script:RepoRoot $relative
            $destination = Join-Path $Path $relative
            [IO.File]::WriteAllText($destination, ([IO.File]::ReadAllText($source) -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
        }
        [IO.File]::WriteAllText((Join-Path $Path '.gitattributes'), "*.json text eol=lf`n*.ps1 text eol=lf`n*.cmd text eol=crlf`n*.bat text eol=crlf`n*.sh text eol=lf`n")
        [IO.File]::WriteAllText((Join-Path $Path 'data.json'), "{`n  `"a`": 1`n}`n")
        Invoke-Git -Root $Path init -q | Out-Null
        Invoke-Git -Root $Path add . | Out-Null
        Invoke-Git -Root $Path commit -q -m seed | Out-Null
        return $Path
    }

    function Invoke-TestRepository {
        param(
            [string]$Root,
            [string]$Skip = $script:SkippedSteps,
            [string[]]$Extra = @(),
            [hashtable]$Environment = @{}
        )
        $saved = @{}
        foreach ($name in $Environment.Keys) {
            $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, [string]$Environment[$name], 'Process')
        }
        try {
            return Invoke-Captured -File $script:Shell -Arguments (@('-NoProfile', '-File', (Join-Path $Root 'scripts\test.ps1'), '-Tag', 't', '-Skip', $Skip) + $Extra) -WorkingDirectory $Root
        } finally {
            foreach ($name in $saved.Keys) {
                [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
            }
        }
    }
}

Describe 'scripts/test.ps1 repository preflight' {
    It 'reports CRLF worktree bytes without rewriting them' {
        $repo = New-TestRepository -Path (Join-Path $TestDrive 'lf-index')
        (Invoke-TestRepository -Root $repo).ExitCode | Should -Be 0
        [IO.File]::WriteAllText((Join-Path $repo 'data.json'), "{`r`n  `"a`": 1`r`n}`r`n")
        $dirty = Invoke-TestRepository -Root $repo
        $dirty.ExitCode | Should -Not -Be 0
        $dirty.Output | Should -Match 'EOL_CRLF data.json \(index lf\)'
        $dirty.Output | Should -Match 'Eol preflight failed'
        $dirty.Output | Should -Not -Match 'reset --hard'
        [IO.File]::ReadAllText((Join-Path $repo 'data.json')) | Should -Match "`r`n"
        $fix = @($dirty.Output -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith('git ') })
        $fix.Count | Should -Be 2
        $fix[0].Trim() | Should -Be 'git rm -q --cached -- data.json'
        $fix[1].Trim() | Should -Be 'git restore --source=HEAD --staged --worktree -- data.json'
    }

    It 'renormalizes CRLF already committed to the index' {
        $repo = New-TestRepository -Path (Join-Path $TestDrive 'crlf-index')
        [IO.File]::WriteAllText((Join-Path $repo 'data.json'), "{`r`n  `"a`": 2`r`n}`r`n")
        '*.ps1 -text' | Set-Content -LiteralPath (Join-Path $repo '.gitattributes') -Encoding UTF8
        Invoke-Git -Root $repo add . | Out-Null
        Invoke-Git -Root $repo commit -q -m 'crlf in index' | Out-Null
        "*.json text eol=lf`n*.ps1 text eol=lf`n*.cmd text eol=crlf`n*.bat text eol=crlf`n*.sh text eol=lf`n" |
            Set-Content -LiteralPath (Join-Path $repo '.gitattributes') -Encoding UTF8
        Invoke-Git -Root $repo add .gitattributes | Out-Null
        Invoke-Git -Root $repo commit -q -m 'lf rule' | Out-Null
        $result = Invoke-TestRepository -Root $repo
        $result.ExitCode | Should -Not -Be 0
        $result.Output | Should -Match 'EOL_CRLF data.json \(index crlf\)'
        $result.Output | Should -Match 'git add --renormalize -- data.json'
        $result.Output | Should -Match 'git commit -m ''Renormalize line endings'' -- data.json'
    }

    It 'checks missing, edited, and locked WAV bytes in WithWav mode' {
        $repo = New-TestRepository -Path (Join-Path $TestDrive 'wav-mode')
        $library = Join-Path $repo 'apps\desktop\public\library'
        $schema = Join-Path $repo 'schemas\asset-object'
        New-Item -ItemType Directory -Force -Path $library, $schema | Out-Null
        $wav = [Text.Encoding]::UTF8.GetBytes('RIFF fake wav bytes')
        $wavHashSource = Join-Path $TestDrive 'wav-hash-source.wav'
        [IO.File]::WriteAllBytes($wavHashSource, $wav)
        $sha = (Get-FileHash -LiteralPath $wavHashSource -Algorithm SHA256).Hash.ToLowerInvariant()
        @{ media = @{ 'a.wav' = @{ sha256 = $sha } } } | ConvertTo-Json -Depth 5 |
            Set-Content -LiteralPath (Join-Path $schema 'media.lock.json') -Encoding UTF8

        (Invoke-TestRepository -Root $repo -Extra @('-WithWav')).Output | Should -Match 'WAV_MISSING a.wav'
        [IO.File]::WriteAllBytes((Join-Path $library 'a.wav'), $wav + [byte]120)
        (Invoke-TestRepository -Root $repo -Extra @('-WithWav')).Output | Should -Match 'WAV_MISMATCH a.wav'
        [IO.File]::WriteAllBytes((Join-Path $library 'a.wav'), $wav)
        $ok = Invoke-TestRepository -Root $repo -Extra @('-WithWav')
        $ok.ExitCode | Should -Be 0
        $ok.Output | Should -Match '1/1 staged WAVs match media.lock.json sha256'
        (Invoke-TestRepository -Root $repo -Extra @('-WithWav', '-FromLock')).Output | Should -Match 'exclude each other'
    }
}

Describe 'scripts/test.ps1 script-sprawl gate' {
    It 'finds stray executable scripts and ignores Rust inner attributes' {
        $repo = New-TestRepository -Path (Join-Path $TestDrive 'sprawl')
        '#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]' |
            Set-Content -LiteralPath (Join-Path $repo 'main.rs') -Encoding UTF8
        $clean = Invoke-TestRepository -Root $repo -Skip 'Pssa,Pester,Regen,Cargo,Npm,Python'
        $clean.ExitCode | Should -Be 0
        $clean.Output | Should -Match 'Sprawl PASS'

        New-Item -ItemType Directory -Force -Path (Join-Path $repo 'tools') | Out-Null
        [IO.File]::WriteAllText((Join-Path $repo 'scripts\build-linux.sh'), "#!/usr/bin/env bash`nnpx --yes @tauri-apps/cli@2.12.1 build --bundles deb`n", (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText((Join-Path $repo 'tools\deploy'), "#!/bin/sh`necho hi`n", (New-Object Text.UTF8Encoding($false)))
        'console.log(1)' | Set-Content -LiteralPath (Join-Path $repo 'helper.mjs') -Encoding UTF8
        $bad = Invoke-TestRepository -Root $repo -Skip 'Pssa,Pester,Regen,Cargo,Npm,Python'
        $bad.ExitCode | Should -Not -Be 0
        $bad.Output | Should -Match 'SPRAWL outside-allowlist scripts/build-linux.sh'
        $bad.Output | Should -Match 'SPRAWL tauri-build scripts/build-linux.sh'
        $bad.Output | Should -Match 'SPRAWL outside-allowlist tools/deploy'
        $bad.Output | Should -Match 'SPRAWL outside-allowlist helper.mjs'
        $bad.Output | Should -Not -Match 'outside-allowlist main.rs'
    }
}

Describe 'scripts/test.ps1 native-first documentation' {
    It 'documents Pester as the PowerShell runner and checks cube bytes from lock mode' {
        $text = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'scripts\test.ps1') -Raw
        $help = $text.Substring(0, $text.IndexOf('#>'))
        $help | Should -Match 'Pester  PowerShell behavior tests'
        $help | Should -Not -Match 'cannot see a hand-edited cube'
        $fromLock = $help.Substring($help.IndexOf("`n  -FromLock"), $help.IndexOf("`n  -WithWav") - $help.IndexOf("`n  -FromLock"))
        $fromLock | Should -Match 'cubes'
        $fromLock | Should -Match 'hand-edited cube'
    }
}
