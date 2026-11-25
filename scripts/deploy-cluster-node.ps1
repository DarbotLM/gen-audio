[CmdletBinding()]
param(
    [ValidateSet('all', 'dayour', 'darbotlm', 'darbotlabs')]
    [string]$Node = 'all',
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'runtimes\cluster.windows.json'),
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
    $script = @"
`$ErrorActionPreference = 'Stop'
`$root = '$quotedRoot'
if (Test-Path -LiteralPath (Join-Path `$root '.git')) {
  git -C `$root fetch origin
  if (`$LASTEXITCODE -ne 0) { throw 'git fetch failed' }
  git -C `$root pull --ff-only
  if (`$LASTEXITCODE -ne 0) { throw 'git pull --ff-only failed' }
} else {
  New-Item -ItemType Directory -Path (Split-Path -Parent `$root) -Force | Out-Null
  git clone https://github.com/darbotlabs/gen-audio.git `$root
  if (`$LASTEXITCODE -ne 0) { throw 'git clone failed' }
}
if (-not `$SkipRuntimeInstall) {
  Set-Location -LiteralPath `$root
  & uv venv --python 3.15.2 --seed .venv
  & uv pip install --python .venv\Scripts\python.exe -e '.[dev]'
  & uv venv --python 3.12 --seed .venv-vibevoice-hybrid
  & uv pip install --python .venv-vibevoice-hybrid\Scripts\python.exe -r runtimes\requirements-vibevoice-hybrid.txt --index-strategy unsafe-best-match
  & uv venv --python 3.12 --seed .venv-personaplex-hybrid
  & uv pip install --python .venv-personaplex-hybrid\Scripts\python.exe -r runtimes\requirements-personaplex-hybrid.txt
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

