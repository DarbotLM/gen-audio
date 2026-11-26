#Requires -Version 7.0
<#
.SYNOPSIS
PowerShell-native VibeVoice 7B control and tensor orchestration.

.DESCRIPTION
Uses the pinned binary PowerTorch module for validation and execution. The
production path refuses unsupported model capabilities; it never launches
Python. C#/F# model kernels are invoked through wingen.audio.exe as they are
brought forward.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Status', 'Preflight', 'Load', 'Unload', 'Generate')]
    [string]$Action,
    [string]$ModelDir = 'G:\models\VibeVoice-7B',
    [string]$Tokenizer = '',
    [string]$VoiceRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'voice-library'),
    [string]$RequestPath,
    [string]$PowerTorchRoot
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'preflight.ps1')

if (-not $Tokenizer) { $Tokenizer = Join-Path $ModelDir 'tokenizer' }
$request = if ($RequestPath) {
    ConvertFrom-WingenVibeVoiceRequest -Path $RequestPath
} else {
    ConvertTo-WingenVibeVoiceRequest -ModelDir $ModelDir -Tokenizer $Tokenizer
}

switch ($Action) {
    'Status' {
        $execution = Get-WingenVibeVoiceExecution -PowerTorchRoot $PowerTorchRoot
        [pscustomobject]@{
            ok = $true
            engine = 'vibevoice'
            loaded = $false
            phase = if ($execution.ready) { 'ready' } else { 'capability_gap' }
            runtime = 'powertorch'
            execution = $execution
        } | ConvertTo-Json -Depth 32 -Compress
        exit 0
    }
    'Preflight' {
        $receipt = Invoke-WingenVibeVoicePreflight -Request $request -VoiceRoot $VoiceRoot `
            -PowerTorchRoot $PowerTorchRoot -LoadOnly:($null -eq $request.scriptText)
        $receipt | ConvertTo-Json -Depth 32 -Compress
        if (-not $receipt.ready) { exit 2 }
        exit 0
    }
    'Unload' {
        [pscustomobject]@{ ok = $true; engine = 'vibevoice'; loaded = $false; phase = 'unloaded'; runtime = 'powertorch' } |
            ConvertTo-Json -Depth 8 -Compress
        exit 0
    }
    default {
        $receipt = Invoke-WingenVibeVoicePreflight -Request $request -VoiceRoot $VoiceRoot `
            -PowerTorchRoot $PowerTorchRoot -LoadOnly:($Action -eq 'Load')
        if (-not $receipt.ready) {
            $receipt | ConvertTo-Json -Depth 32 -Compress
            exit 2
        }
        throw "PowerTorch VibeVoice '$Action' graph is not implemented after preflight; remaining work belongs to the pinned wingen.audio PowerTorch runtime."
    }
}
