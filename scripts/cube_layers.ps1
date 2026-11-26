#Requires -Version 7.0
<#
.SYNOPSIS
Generate a Library revision-3 cube with the .NET 11 F# implementation.

.DESCRIPTION
This is the native replacement for `python scripts/cube_revision.py layers`.
WAV parsing, STFT, four-layer math, preview selection, JSON, and PNG rendering
run inside wingen.audio.exe; PowerShell owns path resolution and exit-code
propagation only.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [Alias('Input')]
    [string]$InputPath,
    [Parameter(Mandatory, Position = 1)]
    [string]$Output,
    [Parameter(Mandatory)]
    [string]$Stem,
    [Parameter(Mandatory)]
    [string]$Engine,
    [string]$Label,
    [string]$Png,
    [string]$NativeHost = ''
)

$ErrorActionPreference = 'Stop'
$cubeInvocation = @{
    InputPath = $InputPath
    Output = $Output
    Stem = $Stem
    Engine = $Engine
    Label = $Label
    Png = $Png
    NativeHost = $NativeHost
}
. (Join-Path $PSScriptRoot 'model_clients.ps1')
$InputPath = $cubeInvocation.InputPath
$Output = $cubeInvocation.Output
$Stem = $cubeInvocation.Stem
$Engine = $cubeInvocation.Engine
$Label = $cubeInvocation.Label
$Png = $cubeInvocation.Png
$NativeHost = $cubeInvocation.NativeHost
$native = Resolve-WingenAudioNativeHost -ExplicitPath $NativeHost
if (-not $native) {
    throw 'wingen.audio.exe is not published. Build src\wingen.audio\wingen.audio.csproj first.'
}
$repo = Split-Path -Parent $PSScriptRoot
$resolvedInput = if ([IO.Path]::IsPathFullyQualified($InputPath)) { $InputPath } else { Join-Path $repo $InputPath }
$outputPath = if ([IO.Path]::IsPathFullyQualified($Output)) { $Output } else { Join-Path $repo $Output }
if (-not (Test-Path -LiteralPath $resolvedInput -PathType Leaf)) {
    throw "Input WAV not found: $resolvedInput"
}
$arguments = @(
    'cube-layers',
    '--input', ([IO.Path]::GetFullPath($resolvedInput)),
    '--output', ([IO.Path]::GetFullPath($outputPath)),
    '--stem', $Stem,
    '--engine', $Engine,
    '--repo-root', $repo
)
if ($Label) { $arguments += @('--label', $Label) }
if ($Png) {
    $pngPath = if ([IO.Path]::IsPathFullyQualified($Png)) { $Png } else { Join-Path $repo $Png }
    $arguments += @('--png', ([IO.Path]::GetFullPath($pngPath)))
}
& $native @arguments
if ($LASTEXITCODE -ne 0) { throw "wingen.audio cube-layers exited $LASTEXITCODE." }
