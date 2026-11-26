[CmdletBinding()]
param(
    [ValidateSet('all', 'darbot', 'dayour', 'darbotlm', 'darbotlabs')]
    [string]$Node = 'all',
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'runtimes\cluster.windows.json'),
    [ValidatePattern('^[A-Za-z0-9._/-]+$')]
    [string]$GitRef,
    [switch]$StageModels,
    [switch]$SkipRuntimeInstall
)

$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'Cluster deployment requires PowerShell 7 or newer.'
}
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$nodes = @($config.nodes | Where-Object { $Node -eq 'all' -or $_.id -eq $Node })
if ($nodes.Count -eq 0) { throw "No cluster node matched '$Node'." }
if ($StageModels) {
    throw 'StageModels is intentionally blocked: model weights are external assets and must be staged with the operator-approved model-copy process.'
}

function ConvertTo-EncodedCommand([string]$Script) {
    [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
}

$results = foreach ($node in $nodes) {
    $quotedRoot = $node.root.Replace("'", "''")
    $powerTorchCommit = (Get-Content -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'runtimes\powertorch.version.json') -Raw |
        ConvertFrom-Json).commit
    $powerDrive = [IO.Path]::GetPathRoot($node.root)
    $quotedPowerRoot = (Join-Path $powerDrive 'PowerTorch').Replace("'", "''")
    $script = @"
`$ErrorActionPreference = 'Stop'
`$root = '$quotedRoot'
`$powerRoot = '$quotedPowerRoot'
if (Test-Path -LiteralPath (Join-Path `$root '.git')) {
  git -C `$root fetch origin
  if (`$LASTEXITCODE -ne 0) { throw 'git fetch failed' }
  if ('$GitRef') {
    git -C `$root show-ref --verify --quiet 'refs/heads/$GitRef'
    if (`$LASTEXITCODE -eq 0) {
      git -C `$root switch '$GitRef'
    } else {
      git -C `$root switch -c '$GitRef' --track 'origin/$GitRef'
    }
    if (`$LASTEXITCODE -ne 0) { throw "git switch $GitRef failed; preserve local work and resolve the checkout before deployment" }
  }
  git -C `$root pull --ff-only
  if (`$LASTEXITCODE -ne 0) { throw 'git pull --ff-only failed' }
} else {
  New-Item -ItemType Directory -Path (Split-Path -Parent `$root) -Force | Out-Null
  git clone https://github.com/darbotlabs/gen-audio.git `$root
  if (`$LASTEXITCODE -ne 0) { throw 'git clone failed' }
}
if (Get-Command cmake.exe -ErrorAction SilentlyContinue) {
  & winget upgrade --id Kitware.CMake -e --source winget --silent --accept-package-agreements --accept-source-agreements 2>`$null
} else {
  & winget install --id Kitware.CMake -e --source winget --silent --accept-package-agreements --accept-source-agreements
  if (`$LASTEXITCODE -ne 0) { throw 'Kitware.CMake installation failed' }
}
if (-not (Get-Command cmake.exe -ErrorAction SilentlyContinue)) { throw 'Kitware.CMake is still unavailable on PATH' }
if (Test-Path -LiteralPath (Join-Path `$powerRoot '.git')) {
  git -C `$powerRoot fetch origin --prune
  if (`$LASTEXITCODE -ne 0) { throw 'PowerTorch fetch failed' }
  git -C `$powerRoot pull --ff-only origin main
  if (`$LASTEXITCODE -ne 0) { throw 'PowerTorch has local work or diverged history; preserve and merge it before deployment' }
} else {
  New-Item -ItemType Directory -Path (Split-Path -Parent `$powerRoot) -Force | Out-Null
  git clone https://github.com/DarbotLM/PowerTorch.git `$powerRoot
  if (`$LASTEXITCODE -ne 0) { throw 'PowerTorch clone failed' }
}
`$actualPowerCommit = git -C `$powerRoot rev-parse HEAD
if (`$actualPowerCommit -ne '$powerTorchCommit') { throw "PowerTorch commit is `$actualPowerCommit, expected $powerTorchCommit" }
`$powerLog = Join-Path `$env:TEMP 'PowerTorch-build.log'
& (Join-Path `$powerRoot 'scripts\Build-PowerTorch.ps1') -Configuration Release *> `$powerLog
if (`$LASTEXITCODE -ne 0) { throw "PowerTorch build failed; see `$powerLog" }
Import-Module (Join-Path `$powerRoot 'artifacts\module\PowerTorch.psd1') -Force
`$powerHealth = Test-PowerTorch -DirectML
if (-not `$powerHealth.Healthy) { throw "PowerTorch health failed: `$(`$powerHealth.Diagnostic)" }
if (-not `$SkipRuntimeInstall) {
  Set-Location -LiteralPath `$root
  & uv venv --python 3.15.2 --seed .venv
  if (`$LASTEXITCODE -ne 0) { throw 'Gen-Audio app venv creation failed' }
  & uv pip install --python .venv\Scripts\python.exe -e '.[dev]'
  if (`$LASTEXITCODE -ne 0) { throw 'Gen-Audio app dependency installation failed' }
  & uv venv --python 3.12 --seed .venv-vibevoice-hybrid
  if (`$LASTEXITCODE -ne 0) { throw 'VibeVoice worker venv creation failed' }
  & uv pip install --python .venv-vibevoice-hybrid\Scripts\python.exe -r runtimes\requirements-vibevoice-hybrid.txt --index-strategy unsafe-best-match
  if (`$LASTEXITCODE -ne 0) { throw 'VibeVoice worker dependency installation failed' }
  & uv venv --python 3.12 --seed .venv-personaplex-hybrid
  if (`$LASTEXITCODE -ne 0) { throw 'PersonaPlex worker venv creation failed' }
  & uv pip install --python .venv-personaplex-hybrid\Scripts\python.exe -r runtimes\requirements-personaplex-hybrid.txt
  if (`$LASTEXITCODE -ne 0) { throw 'PersonaPlex worker dependency installation failed' }
}
& (Join-Path `$root 'scripts\start-speech-7b.ps1') -Engine both -SkipDlmSpeech
"@
    $skipRuntimeInstallLiteral = if ($SkipRuntimeInstall) { '$true' } else { '$false' }
    $script = $script.Replace('$SkipRuntimeInstall', $skipRuntimeInstallLiteral)
    $encoded = ConvertTo-EncodedCommand $script
    if ($node.id -eq $env:COMPUTERNAME.ToLowerInvariant()) {
        $output = & 'C:\Program Files\PowerShell\7\pwsh.exe' `
            -NoProfile -NonInteractive -EncodedCommand $encoded
    } else {
        $output = & ssh -T -o BatchMode=yes -o IdentitiesOnly=yes $node.ssh `
            pwsh.exe -NoProfile -NonInteractive -EncodedCommand $encoded
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Deployment to $($node.id) failed (exit $LASTEXITCODE)."
    }
    [pscustomobject]@{
        node = $node.id
        root = $node.root
        output = @($output)[-1]
    }
}
$results | ConvertTo-Json -Depth 8

