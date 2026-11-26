#Requires -Version 7.0

$script:WingenVibeVoiceLimits = [pscustomobject]@{
    MaxTextCharacters = 8192
    MaxTextBytes = 32768
    MaxReferenceSeconds = 30.0
    MaxSeconds = 60.0
    MaxNewTokens = 1024
    MaxInputTokens = 4096
    MaxInferenceSteps = 50
    MaxSpeakers = 4
    SampleRate = 24000
}

function ConvertTo-WingenVibeVoiceError {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Code,
        [Parameter(Mandatory = $true)][string]$Message,
        [object]$Details
    )
    [pscustomobject]@{ code = $Code; message = $Message; details = $Details }
}

function Test-WingenVibeVoiceScript {
    [CmdletBinding()]
    param(
        [AllowNull()][string]$ScriptText,
        [ValidateRange(1, 4)][int]$VoiceCount
    )
    $lines = @(
        ([string]$ScriptText) -split "`r?`n" |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    )
    if ($lines.Count -eq 0) {
        throw 'VibeVoice requires nonempty scriptText.'
    }
    $parsedLines = @($lines | ForEach-Object {
        if ($_ -match '^Speaker\s+([1-4]):\s*(\S.*)$') {
            [pscustomobject]@{ Speaker = [int]$Matches[1]; Text = $Matches[2] }
        } else {
            $null
        }
    })
    if (@($parsedLines | Where-Object { $_ }).Count -eq 0) {
        if ($VoiceCount -ne 1) { throw 'Plain text requires exactly one voice reference.' }
        if (@($lines | Where-Object { $_ -match '^Speaker\s+\d+\s*:' }).Count) {
            throw 'Speaker IDs must be 1..4 with nonempty text.'
        }
        return "Speaker 1: $($lines -join ' ')"
    }
    if (@($parsedLines | Where-Object { $_ -eq $null }).Count -gt 0) {
        throw 'Every script line must use Speaker N: text.'
    }
    $ids = @($parsedLines.Speaker | Sort-Object -Unique)
    if (($ids -join ',') -ne ((1..$VoiceCount) -join ',')) {
        throw 'Use contiguous Speaker 1..N and one voice reference per speaker.'
    }
    return ($parsedLines | ForEach-Object { "Speaker $($_.Speaker): $($_.Text)" }) -join "`n"
}

function ConvertTo-WingenVibeVoiceRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ModelDir,
        [string]$Tokenizer,
        [string]$ScriptText,
        [string[]]$VoiceRefs = @(),
        [string]$Output,
        [ValidateRange(1, 1024)][int]$MaxNewTokens = 256,
        [ValidateRange(0.001, 60.0)][double]$MaxSeconds = 30.0,
        [ValidateRange(0.001, 3600.0)][double]$TimeoutSeconds = 600.0,
        [ValidateRange(0, 4294967295)][long]$Seed = 0,
        [ValidateRange(0.0, 4.0)][double]$CfgScale = 1.3,
        [ValidateRange(1, 50)][int]$InferenceSteps = 10,
        [hashtable]$Execution = @{},
        [string[]]$AgentNames = @()
    )
    $text = if ($ScriptText) {
        if ($VoiceRefs.Count -lt 1 -or $VoiceRefs.Count -gt 4) {
            throw 'VibeVoice requires 1..4 voiceRefs.'
        }
        Test-WingenVibeVoiceScript -ScriptText $ScriptText -VoiceCount $VoiceRefs.Count
    } else {
        $null
    }
    if ($ScriptText -and ([Text.Encoding]::UTF8.GetByteCount($ScriptText) -gt $script:WingenVibeVoiceLimits.MaxTextBytes -or
        $ScriptText.Length -gt $script:WingenVibeVoiceLimits.MaxTextCharacters)) {
        throw 'scriptText exceeds 8192 characters or 32768 UTF-8 bytes.'
    }
    [pscustomobject]@{
        schema_version = '1.0'
        engine = 'vibevoice'
        model_dir = $ModelDir
        tokenizer = if ($Tokenizer) { $Tokenizer } else { Join-Path $ModelDir 'tokenizer' }
        scriptText = $text
        voiceRefs = @($VoiceRefs)
        output = $Output
        maxNewTokens = $MaxNewTokens
        maxSeconds = $MaxSeconds
        timeoutSeconds = $TimeoutSeconds
        seed = $Seed
        cfgScale = $CfgScale
        inferenceSteps = $InferenceSteps
        execution = $Execution
        agentNames = @($AgentNames)
    }
}

function ConvertFrom-WingenVibeVoiceRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "VibeVoice request not found: $Path"
    }
    $payload = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -Depth 32
    ConvertTo-WingenVibeVoiceRequest -ModelDir $payload.model_dir -Tokenizer $payload.tokenizer `
        -ScriptText $payload.scriptText -VoiceRefs @($payload.voiceRefs) -Output $payload.output `
        -MaxNewTokens ([int]$payload.maxNewTokens) -MaxSeconds ([double]$payload.maxSeconds) `
        -TimeoutSeconds ([double]$payload.timeoutSeconds) -Seed ([long]$payload.seed) `
        -CfgScale ([double]$payload.cfgScale) -InferenceSteps ([int]$payload.inferenceSteps) `
        -Execution ($payload.execution | ConvertTo-Json -Compress | ConvertFrom-Json -AsHashtable) `
        -AgentNames @($payload.agentNames)
}
