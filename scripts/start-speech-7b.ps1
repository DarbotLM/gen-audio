[CmdletBinding()]
param(
    [string]$OutputRoot = "\\fschia\genai-media\audio",
    [string]$VoiceRoot = "\\fschia\genai-media\audio\voice-library",
    [string]$VibeModelDir = "\\fschia\dlm-lib\MSFT\VibeVoice\7B",
    [string]$PersonaModelDir = "\\fschia\dlm-lib\NVIDIA\Personaplex-7B",
    [ValidateSet("both", "vibevoice", "personaplex")]
    [string]$Engine = "both",
    [ValidateSet("auto", "cuda", "cpu")]
    [string]$Backend = "auto",
    [switch]$Preload,
    [switch]$SkipDlmSpeech,
    [switch]$PersistEnvironment,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeRoot = Join-Path $RepoRoot "artifacts\runtime\7b"
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
if ($ReplaceExisting) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '-ReplaceExisting requires an elevated PowerShell session.'
    }
    foreach ($taskName in @('GenAudio-vibevoice-7B', 'GenAudio-personaplex-7B')) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    foreach ($pidFile in @(Get-ChildItem -LiteralPath $RuntimeRoot -Filter '*.control.pid' -File -ErrorAction SilentlyContinue)) {
        $controlPid = [int](Get-Content -LiteralPath $pidFile.FullName -Raw).Trim()
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$controlPid" -ErrorAction SilentlyContinue
        if ($process -and $process.Name -eq 'wingen.audio.exe') {
            Stop-Process -Id $controlPid -Force -ErrorAction SilentlyContinue
        }
    }
    foreach ($listener in @(Get-NetTCPConnection -LocalPort 8996, 8997, 18996, 18997 -State Listen -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $listeners = @(Get-NetTCPConnection -LocalPort 8996, 8997, 18996, 18997 -State Listen -ErrorAction SilentlyContinue)
        if ($listeners.Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if ($listeners.Count -gt 0) {
        throw 'Existing Gen-Audio worker listeners did not stop within 30 seconds.'
    }
}

function Start-ResidentWorker {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [string]$Name,
        [string]$Python,
        [string[]]$Arguments,
        [int]$Port
    )
    try {
        $existing = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/status" -TimeoutSec 2
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($existing -and $listener) {
            return Get-Process -Id $listener.OwningProcess
        }
    } catch {
        Write-Verbose "No existing $Name worker is ready on port $Port."
    }
    $stdout = Join-Path $RuntimeRoot "$Name.stdout.log"
    $stderr = Join-Path $RuntimeRoot "$Name.stderr.log"
    $env:PYTHONPATH = Join-Path $RepoRoot "src"
    $argumentsJson = ConvertTo-Json -InputObject @($Arguments) -Depth 4 -Compress
    $argumentsBase64 = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($argumentsJson)
    )
    $hostScript = Join-Path $PSScriptRoot "run-resident-worker.ps1"
    $commandLine = (
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        '-File "' + $hostScript + '" ' +
        '-Python "' + $Python + '" ' +
        '-ArgumentsBase64 "' + $argumentsBase64 + '" ' +
        '-Stdout "' + $stdout + '" ' +
        '-Stderr "' + $stderr + '"'
    )
    $taskName = "GenAudio-$Name-7B"
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction `
        -Execute 'C:\Program Files\PowerShell\7\pwsh.exe' `
        -Argument $commandLine `
        -WorkingDirectory $RepoRoot
    $principal = New-ScheduledTaskPrincipal `
        -UserId $currentUser `
        -LogonType S4U `
        -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval ([TimeSpan]::FromMinutes(1))
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Principal $principal `
        -Settings $settings | Out-Null
    Start-ScheduledTask -TaskName $taskName
    $process = $null
    $deadline = (Get-Date).AddMinutes(5)
    do {
        Start-Sleep -Milliseconds 500
        $task = Get-ScheduledTask -TaskName $taskName
        if ($task.State -eq "Ready") {
            $info = Get-ScheduledTaskInfo -TaskName $taskName
            if ($info.LastTaskResult -ne 267009) {
                throw "$Name task exited $($info.LastTaskResult); inspect $stderr"
            }
        }
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/status" -TimeoutSec 2
            if ($null -ne $status) {
                $listener = Get-NetTCPConnection -LocalPort $Port -State Listen |
                    Select-Object -First 1
                $process = Get-Process -Id $listener.OwningProcess
                $process.Id | Set-Content -LiteralPath (Join-Path $RuntimeRoot "$Name.pid") -Encoding ascii
                return $process
            }
        } catch {
            Write-Verbose "$Name is still starting."
        }
    } while ((Get-Date) -lt $deadline)
    throw "$Name did not publish port $Port"
}

function Invoke-ResidentLoad {
    param([int]$Port, [string]$Name)
    $result = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:$Port/v1/load" `
        -ContentType "application/json" `
        -Body "{}" `
        -TimeoutSec 1800
    if ($result.loaded -ne $true) {
        throw "$Name did not load: $($result | ConvertTo-Json -Compress)"
    }
}

$VibePython = Join-Path $RepoRoot ".venv-vibevoice-hybrid\Scripts\python.exe"
$PersonaPython = Join-Path $RepoRoot ".venv-personaplex-hybrid\Scripts\python.exe"
$DlmPython = Join-Path $RepoRoot ".venv-dlm-speech\Scripts\dlm-speech.exe"
foreach ($runtime in @($VibePython, $PersonaPython)) {
    if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) {
        throw "Missing isolated 7B runtime: $runtime. Run scripts\deploy-cluster-node.ps1 first."
    }
}
$HasCuda = $null -ne (Get-CimInstance Win32_VideoController | Where-Object Name -match "NVIDIA")
if ($Backend -eq "auto") {
    $Backend = if ($HasCuda) { "cuda" } else { "cpu" }
}
$env:GEN_AUDIO_PERSONAPLEX_CACHE_DIR = Join-Path $OutputRoot ".cache\personaplex-voices"
$env:GEN_AUDIO_VIBEVOICE_CPU_THREADS = "24"
$env:GEN_AUDIO_PERSONAPLEX_CPU_THREADS = "24"
$env:GEN_AUDIO_PERSONAPLEX_GPU_LAYERS = "4"

$vibe = $null
if ($Engine -in @("both", "vibevoice")) {
    $vibe = Start-ResidentWorker -Name "vibevoice" -Python $VibePython -Port 18996 -Arguments @(
        "-s", "-P", "-B", "-m", "gen_audio.model_workers.service",
        "--engine", "vibevoice",
        "--model-dir", $VibeModelDir,
        "--tokenizer", (Join-Path $VibeModelDir "tokenizer"),
        "--output-root", $OutputRoot,
        "--voice-root", $VoiceRoot,
        "--port", "18996",
        "--device-index", $(if ($Engine -eq "both" -and (Get-CimInstance Win32_VideoController | Where-Object Name -match "NVIDIA" | Measure-Object).Count -gt 1) { "1" } else { "0" }),
        "--backend", $Backend,
        "--gpu-memory-gib", "4.5",
        "--cpu-memory-gib", "48"
    )
    if ($Preload) {
        Invoke-ResidentLoad -Port 18996 -Name "vibevoice"
    }
    & (Join-Path $PSScriptRoot 'service.ps1') -Engine vibevoice -Action Start `
        -Port 8996 -BackendPort 18996 -TimeoutSeconds 1800 | Out-Null
}

$persona = $null
if ($Engine -in @("both", "personaplex")) {
    $persona = Start-ResidentWorker -Name "personaplex" -Python $PersonaPython -Port 18997 -Arguments @(
        "-s", "-P", "-B", "-m", "gen_audio.model_workers.service",
        "--engine", "personaplex",
        "--model-dir", $PersonaModelDir,
        "--tokenizer", (Join-Path $PersonaModelDir "tokenizer_spm_32k_3.model"),
        "--output-root", $OutputRoot,
        "--voice-root", $VoiceRoot,
        "--port", "18997",
        "--device-index", "0",
        "--backend", $Backend,
        "--gpu-memory-gib", "5.25",
        "--cpu-memory-gib", "48"
    )
    if ($Preload) {
        Invoke-ResidentLoad -Port 18997 -Name "personaplex"
    }
    & (Join-Path $PSScriptRoot 'service.ps1') -Engine personaplex -Action Start `
        -Port 8997 -BackendPort 18997 -TimeoutSeconds 1800 | Out-Null
}

$dlm = $null
if (-not $SkipDlmSpeech -and $persona) {
    if (-not (Test-Path -LiteralPath $DlmPython -PathType Leaf)) {
        throw "Missing DLM Speech executable: $DlmPython. Use -SkipDlmSpeech or install that isolated runtime."
    }
    $env:DLM_SPEECH_PERSONAPLEX_DEVICE_PROFILE = "gpu"
    $env:DLM_SPEECH_PERSONAPLEX_GPU_ENABLED = "1"
    $env:DLM_SPEECH_PERSONAPLEX_GPU_URL = "ws://127.0.0.1:8997/api/chat"
    $env:DLM_SPEECH_VIBEVOICE_ENABLED = "0"
    $dlm = Start-Process `
        -FilePath $DlmPython `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput (Join-Path $RuntimeRoot "dlm-speech.stdout.log") `
        -RedirectStandardError (Join-Path $RuntimeRoot "dlm-speech.stderr.log") `
        -PassThru
    $dlm.Id | Set-Content -LiteralPath (Join-Path $RuntimeRoot "dlm-speech.pid") -Encoding ascii
}

if ($PersistEnvironment) {
    $settings = [ordered]@{
        GEN_AUDIO_VIBEVOICE_WORKER_URL = "http://127.0.0.1:8996"
        GEN_AUDIO_PERSONAPLEX_WORKER_URL = "http://127.0.0.1:8997"
        GEN_AUDIO_DLM_SPEECH_URL = "http://127.0.0.1:8999"
        GEN_AUDIO_ARTIFACT_DIR = $OutputRoot
        GENAID_VOICES_DIR = $VoiceRoot
        GEN_AUDIO_VIBEVOICE_SIZE = "7B"
        GEN_AUDIO_VIBEVOICE_7B_MODEL = $VibeModelDir
        GEN_AUDIO_PERSONAPLEX_MODEL_DIR = $PersonaModelDir
    }
    foreach ($entry in $settings.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "User")
        Set-Item -Path "Env:$($entry.Key)" -Value $entry.Value
    }
}

[ordered]@{
    vibevoice = if ($vibe) { $vibe.Id } else { $null }
    personaplex = if ($persona) { $persona.Id } else { $null }
    dlm_speech = if ($dlm) { $dlm.Id } else { $null }
    vibevoice_url = "http://127.0.0.1:8996"
    personaplex_url = "http://127.0.0.1:8997"
    dlm_speech_url = "http://127.0.0.1:8999"
    output_root = $OutputRoot
    voice_root = $VoiceRoot
} | ConvertTo-Json
