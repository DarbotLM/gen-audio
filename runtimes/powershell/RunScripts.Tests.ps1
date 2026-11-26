BeforeAll {
    $script:RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $script:Toolbox = Join-Path $script:RepoRoot 'scripts\toolbox.ps1'
}

Describe 'run.ps1 launch planning' {
    BeforeEach {
        $script:Checkout = Join-Path $TestDrive 'checkout'
        $script:Scripts = Join-Path $script:Checkout 'scripts'
        New-Item -ItemType Directory -Force -Path (Join-Path $script:Scripts 'lib') | Out-Null
        foreach ($file in @('run.ps1', 'runtime.json')) {
            Copy-Item -LiteralPath (Join-Path $script:RepoRoot "scripts\$file") -Destination $script:Scripts
        }
        Copy-Item -LiteralPath (Join-Path $script:RepoRoot 'scripts\lib\runtime-config.ps1') -Destination (Join-Path $script:Scripts 'lib')
    }

    It 'reads checkout descriptor ports instead of user environment values' {
        $runtime = Join-Path $script:Checkout 'artifacts\runtime'
        New-Item -ItemType Directory -Force -Path $runtime | Out-Null
        @{
            checkout = "  $($script:Checkout.ToLowerInvariant())\  "
            ports = @{ Node = 18002; Mcp = 18765; Desktop = 18765; Preview = 11420 }
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $runtime 'node-environment.json') -Encoding UTF8

        $saved = $env:GEN_AUDIO_NODE_PORT
        try {
            $env:GEN_AUDIO_NODE_PORT = '9999'
            $plan = (& (Join-Path $script:Scripts 'run.ps1') -Service Node -Check | Out-String) | ConvertFrom-Json
        } finally {
            $env:GEN_AUDIO_NODE_PORT = $saved
        }
        $plan.arguments[-1] | Should -Match 'port=18002'
        $plan.environment.GEN_AUDIO_NODE_PORT | Should -Be '18002'
        $plan.environment.GEN_AUDIO_MCP_ADDR | Should -Be '127.0.0.1:18765'
    }

    It 'names the owner when a requested listener port is occupied' -Skip:($env:OS -ne 'Windows_NT') {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
        try {
            { & (Join-Path $script:Scripts 'run.ps1') -Service Node -Port $port -Check } | Should -Not -Throw
            { & (Join-Path $script:Scripts 'run.ps1') -Service Node -Port $port } |
                Should -Throw "*Port $port is already in use by PID*Stop it or pass -Port <free port>*"
        } finally {
            $listener.Stop()
        }
    }
}

Describe 'toolbox.ps1 ownership contracts' {
    It 'terminates tracked process trees and verifies both port releases' {
        $text = Get-Content -LiteralPath $script:Toolbox -Raw
        $service = $text.Substring($text.IndexOf('function Invoke-NodeService'), $text.IndexOf('function Invoke-ServiceSuite') - $text.IndexOf('function Invoke-NodeService'))
        $service | Should -Match ([regex]::Escape('Complete-ToolboxProcessTree -RootProcessId $existing.Id'))
        $service | Should -Match ([regex]::Escape('Complete-ToolboxProcessTree -RootProcessId $process.Id'))
        $service | Should -Match ([regex]::Escape('-AdditionalProcessIds @($trackedChildIds)'))
        ([regex]::Matches($service, 'Wait-ToolboxPortReleased -Port \$Port')).Count | Should -BeGreaterOrEqual 2
        $service | Should -Not -Match 'Stop-Process -Id \$process\.Id'
    }

    It 'requires the operator to explicitly name each package agreement' {
        $text = Get-Content -LiteralPath $script:Toolbox -Raw
        $text | Should -Match '\[string\[\]\]\$AcceptPackageAgreement = @\(\)'
        $block = $text.Substring($text.IndexOf('function Install-WingetPackage'), $text.IndexOf('function Test-Checkout') - $text.IndexOf('function Install-WingetPackage'))
        $gate = $block.IndexOf('if ($AcceptPackageAgreement -notcontains $Id)')
        $accept = $block.IndexOf("'--accept-package-agreements'")
        $gate | Should -BeGreaterThan -1
        $gate | Should -BeLessThan $accept
        $block.Substring($gate, $accept - $gate) | Should -Match 'throw \('
        ([regex]::Matches($text, '--accept-package-agreements')).Count | Should -Be 1
    }

    It 'keeps package installation verified and configurable' {
        $text = Get-Content -LiteralPath $script:Toolbox -Raw
        $text | Should -Match ([regex]::Escape('$script:WingetNoApplicableInstaller = -1978335216'))
        $install = $text.Substring($text.IndexOf('function Install-WingetPackage'), $text.IndexOf('function Test-Checkout') - $text.IndexOf('function Install-WingetPackage'))
        $install | Should -Match '\$global:LASTEXITCODE -ne \$script:WingetNoApplicableInstaller'
        $uv = $text.Substring($text.IndexOf('function Install-Uv'), $text.IndexOf('function Install-DependencySet') - $text.IndexOf('function Install-Uv'))
        $uv.IndexOf("'pip', 'install'") | Should -BeLessThan $uv.IndexOf("Install-WingetPackage -Id 'astral-sh.uv'")
        $text | Should -Match '\$release -notmatch ''\^final '''
        $text | Should -Match "'3.15.2'"
        ([regex]::Matches($text, "'3\.15\.2'")).Count | Should -Be 1
        $pip = $text.Substring($text.IndexOf('$pipArguments = @('), $text.IndexOf('Invoke-External $python $pipArguments') - $text.IndexOf('$pipArguments = @('))
        $pip | Should -Match "'--require-hashes', '--ignore-requires-python'"
        $text | Should -Not -Match '--trusted-host'
    }

    It 'logs native stderr without aborting on Windows PowerShell' {
        $text = Get-Content -LiteralPath $script:Toolbox -Raw
        $block = $text.Substring($text.IndexOf('function Invoke-External'), $text.IndexOf('function Install-WingetPackage') - $text.IndexOf('function Invoke-External'))
        $block.IndexOf('$ErrorActionPreference = ''Continue''') | Should -BeLessThan $block.IndexOf('& $FilePath @Arguments 2>&1')
        $block | Should -Match ([regex]::Escape('$ErrorActionPreference = $previousPreference'))
    }

    It 'promotes runtime library functions and never leaves empty process variables' {
        $text = Get-Content -LiteralPath $script:Toolbox -Raw
        $initialize = $text.Substring($text.IndexOf('function Initialize-Repository'), $text.IndexOf('function Initialize-PackageNetwork') - $text.IndexOf('function Initialize-Repository'))
        $initialize.IndexOf('. $runtimeLibrary') | Should -BeGreaterThan -1
        $initialize.IndexOf('. $runtimeLibrary') | Should -BeLessThan $initialize.IndexOf('Set-Item -Path "function:script:$($definition.Name)"')
        foreach ($name in @('toolbox.ps1', 'run.ps1')) {
            (Get-Content -LiteralPath (Join-Path $script:RepoRoot "scripts\$name") -Raw) |
                Should -Not -Match '\[Environment\]::SetEnvironmentVariable\('
        }
    }

    It 'promotes a dot-sourced library function into script scope' {
        $library = Join-Path $TestDrive 'lib.ps1'
        Set-Content -LiteralPath $library -Value "function Get-Probe { 'probe-ok' }" -Encoding UTF8
        function Initialize-Thing {
            . $library
            $libraryAst = [Management.Automation.Language.Parser]::ParseFile($library, [ref]$null, [ref]$null)
            foreach ($definition in $libraryAst.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
                Set-Item -Path "function:script:$($definition.Name)" -Value (Get-Item -LiteralPath "function:$($definition.Name)").ScriptBlock
            }
        }
        Initialize-Thing
        Get-Probe | Should -Be 'probe-ok'
    }

    It 'uses Windows PowerShell 5.1-safe Node archive extraction' {
        $text = Get-Content -LiteralPath $script:Toolbox -Raw
        $installNode = $text.Substring($text.IndexOf('function Install-Node'))
        $installNode = $installNode.Substring(0, $installNode.IndexOf("`nfunction "))
        $installNode | Should -Not -Match 'Expand-Archive -LiteralPath'
        $installNode | Should -Match ([regex]::Escape("[System.IO.Compression.ZipFile]::ExtractToDirectory(`$archive, (Join-Path `$cache 'extract'))"))
    }

    It 'creates the runtime directory before model downloads' {
        $text = Get-Content -LiteralPath $script:Toolbox -Raw
        $models = $text.Substring($text.IndexOf('Staging checkout-local Kokoro model assets.'))
        $models.IndexOf('New-Item -ItemType Directory -Force -Path $script:RuntimeDir') |
            Should -BeLessThan $models.IndexOf('$download = Join-Path $script:RuntimeDir')
    }
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
