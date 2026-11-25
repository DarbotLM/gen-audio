BeforeAll {
    $script:RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}

Describe 'PowerShell runtime contracts' {
    It 'resolves launch plans from a checkout with spaces for every service' {
        $repo = Join-Path $TestDrive 'checkout with spaces'
        $scripts = Join-Path $repo 'scripts'
        New-Item -ItemType Directory -Force -Path (Join-Path $scripts 'lib') | Out-Null
        Copy-Item -LiteralPath (Join-Path $script:RepoRoot 'scripts\run.ps1') -Destination $scripts
        Copy-Item -LiteralPath (Join-Path $script:RepoRoot 'scripts\runtime.json') -Destination $scripts
        Copy-Item -LiteralPath (Join-Path $script:RepoRoot 'scripts\lib\runtime-config.ps1') -Destination (Join-Path $scripts 'lib')

        $savedModelBase = $env:GENAID_MODEL_BASE
        try {
            $env:GENAID_MODEL_BASE = Join-Path $TestDrive 'stale-models'
            foreach ($case in @(
                @{ Service = 'Mcp'; Port = 8765 }
                @{ Service = 'Node'; Port = 8002 }
                @{ Service = 'Desktop'; Port = 8765 }
                @{ Service = 'Preview'; Port = 1420 }
            )) {
                $json = & (Join-Path $scripts 'run.ps1') -Service $case.Service -Check
                $plan = $json | ConvertFrom-Json
                $plan.environment.GEN_AUDIO_REPO | Should -Be $repo
                $plan.environment.GENAID_MODEL_BASE | Should -Be (Join-Path $repo 'models')
                $plan.environment.GENAID_MODEL_BASE | Should -Be $plan.environment.GEN_AUDIO_MODEL_DIR
                $plan.environment.GEN_AUDIO_WORK_DIR | Should -Be (Join-Path $repo 'artifacts\runtime\mcp')
                $mcpPort = if ($case.Service -in @('Mcp', 'Desktop')) { $case.Port } else { 8765 }
                $plan.environment.GEN_AUDIO_MCP_ADDR | Should -Be "127.0.0.1:$mcpPort"
                Test-Path -LiteralPath (Join-Path $repo 'artifacts') | Should -BeFalse
                if ($case.Service -eq 'Desktop') {
                    [IO.Path]::GetFileName($plan.executable) | Should -Be 'gen-audio.exe'
                } elseif ($case.Service -eq 'Preview') {
                    $plan.arguments | Should -Contain '--strictPort'
                }
            }
        } finally {
            $env:GENAID_MODEL_BASE = $savedModelBase
        }
    }

    It 'removes process variables when their value is null' {
        . (Join-Path $script:RepoRoot 'scripts\lib\runtime-config.ps1')
        $name = 'GA_PESTER_PROBE'
        $saved = [Environment]::GetEnvironmentVariable($name, 'Process')
        try {
            [Environment]::SetEnvironmentVariable($name, 'x', 'Process')
            Set-GenAudioProcessVariable -Name $name -Value $null
            [Environment]::GetEnvironmentVariable($name, 'Process') | Should -BeNullOrEmpty
            Set-GenAudioProcessVariable -Name $name -Value 'y'
            [Environment]::GetEnvironmentVariable($name, 'Process') | Should -Be 'y'
        } finally {
            Set-GenAudioProcessVariable -Name $name -Value $saved
        }
    }
}

Describe 'PowerShell entry points' {
    It 'parses each production PowerShell file' {
        $scripts = @(Get-ChildItem -LiteralPath (Join-Path $script:RepoRoot 'scripts') -Recurse -File -Filter '*.ps1')
        foreach ($script in $scripts) {
            $tokens = $null
            $errors = $null
            [void][Management.Automation.Language.Parser]::ParseFile($script.FullName, [ref]$tokens, [ref]$errors)
            $errors | Should -BeNullOrEmpty -Because "$($script.FullName) must parse"
        }
    }

    It 'keeps native build scripts as the only production build entry points' {
        foreach ($name in @('build-tauri-windows.ps1', 'pack-windows-msix-nuget.ps1')) {
            Test-Path -LiteralPath (Join-Path $script:RepoRoot "scripts\$name") -PathType Leaf | Should -BeTrue
        }
    }
}
