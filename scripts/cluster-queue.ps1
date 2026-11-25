[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("submit", "claim", "heartbeat", "succeed", "fail", "cancel", "get", "stats")]
    [string]$Action,
    [string]$PayloadBase64 = "e30=",
    [string]$StatePath = $(if ($env:GEN_AUDIO_CLUSTER_STATE) {
        $env:GEN_AUDIO_CLUSTER_STATE
    } else {
        "artifacts\cluster"
    })
)

$ErrorActionPreference = "Stop"
$MaxRequestBytes = 64KB
$QueueRoot = [IO.Path]::GetFullPath($StatePath)
$JobsRoot = Join-Path $QueueRoot "jobs"
$KeysRoot = Join-Path $QueueRoot "keys"
New-Item -ItemType Directory -Path $JobsRoot, $KeysRoot -Force | Out-Null

$PayloadBytes = [Convert]::FromBase64String($PayloadBase64)
$Payload = [Text.Encoding]::UTF8.GetString($PayloadBytes) | ConvertFrom-Json

function ConvertTo-Hash {
    param([string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $algorithm.ComputeHash($bytes)
    } finally {
        $algorithm.Dispose()
    }
    return ([BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
}

function Write-Job {
    param(
        [Parameter(Mandatory)][pscustomobject]$Job,
        [Parameter(Mandatory)][string]$Id
    )
    $Job.events = @($Job.events | Select-Object -Last 50)
    $target = Join-Path $JobsRoot "$Id.json"
    $temp = Join-Path $JobsRoot (".$Id-$PID.tmp")
    $Job | ConvertTo-Json -Depth 24 -Compress | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $target -Force
}

function Read-Job {
    param([string]$Id)
    if ($Id -cnotmatch '\A[0-9a-f]{32}\z') { throw 'invalid cluster job id' }
    $path = Join-Path $JobsRoot "$Id.json"
    if (-not (Test-Path -LiteralPath $path)) {
        throw "unknown cluster job: $Id"
    }
    return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Assert-Lease {
    param([pscustomobject]$Job, [string]$Token)
    if ($Job.status -ne "running" -or $Job.leaseToken -cne $Token -or
        $Job.leaseExpiresUnixMs -le [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) {
        throw "cluster job completion lost its lease fence"
    }
}

function Add-Event {
    param([pscustomobject]$Job, [string]$Event, [hashtable]$Details = @{})
    $entry = [ordered]@{
        event = $Event
        at = [DateTimeOffset]::UtcNow.ToString("o")
    }
    foreach ($item in $Details.GetEnumerator()) {
        $entry[$item.Key] = $item.Value
    }
    $Job.events = @($Job.events) + [pscustomobject]$entry
}

function Invoke-Locked {
    param([scriptblock]$Operation)
    $lock = Join-Path $QueueRoot ".queue.lock"
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        try {
            $handle = [IO.FileStream]::new($lock, [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite, [IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose)
            break
        } catch [IO.IOException] {
            if ([DateTime]::UtcNow -ge $deadline) { throw }
            Start-Sleep -Milliseconds 50
        }
    } while ($true)
    try {
        & $Operation
    } finally {
        if ($handle) { $handle.Dispose() }
    }
}

$Result = Invoke-Locked {
    switch ($Action) {
        "submit" {
            if ($Payload.engine -notin @("vibevoice", "personaplex")) {
                throw "unknown cluster engine: $($Payload.engine)"
            }
            $requestJson = $Payload.request | ConvertTo-Json -Depth 24 -Compress
            $requestBytes = [Text.Encoding]::UTF8.GetByteCount($requestJson)
            if ($requestBytes -gt $MaxRequestBytes) {
                throw "cluster request exceeds 64 KiB"
            }
            $requestText = $requestJson.ToLowerInvariant()
            if ($requestText -match '"(base64|audio_base64|audio_bytes|wave_bytes)"' -or
                $requestText -match '"data:') {
                throw "cluster jobs may contain paths, not inline audio"
            }
            $key = if ($Payload.idempotencyKey) {
                [string]$Payload.idempotencyKey
            } else {
                ConvertTo-Hash "$($Payload.engine)`0$requestJson`0$($Payload.preferredNode)"
            }
            $keyHash = ConvertTo-Hash $key
            $keyPath = Join-Path $KeysRoot "$keyHash.id"
            if (Test-Path -LiteralPath $keyPath) {
                return Read-Job (Get-Content -LiteralPath $keyPath -Raw).Trim()
            }
            $maxAttempts = if ($null -eq $Payload.maxAttempts) { 3 } else { [int]$Payload.maxAttempts }
            if ($maxAttempts -lt 1 -or $maxAttempts -gt 20) {
                throw "maxAttempts must be between 1 and 20"
            }
            $priority = if ($null -eq $Payload.priority) { 100 } else { [int]$Payload.priority }
            if ($priority -lt -1000000 -or $priority -gt 1000000) {
                throw "priority must be between -1000000 and 1000000"
            }
            $id = ([guid]::NewGuid().ToString("N"))
            $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            $job = [pscustomobject][ordered]@{
                schemaVersion = "1.0"
                id = $id
                idempotencyKey = $key
                engine = [string]$Payload.engine
                request = $Payload.request
                preferredNode = $Payload.preferredNode
                priority = $priority
                status = "queued"
                attempts = 0
                maxAttempts = $maxAttempts
                leaseOwner = $null
                leaseToken = $null
                leaseExpiresUnixMs = $null
                availableAtUnixMs = $now
                receipt = $null
                lastError = $null
                createdAtUnixMs = $now
                updatedAtUnixMs = $now
                events = @()
            }
            Add-Event $job "submitted"
            Write-Job $job $id
            $id | Set-Content -LiteralPath $keyPath -Encoding ascii -NoNewline
            return $job
        }
        "claim" {
            $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            $engine = $Payload.engine
            $leaseSeconds = if ($null -eq $Payload.leaseSeconds) { 7200 } else { [int]$Payload.leaseSeconds }
            if ($leaseSeconds -lt 5 -or $leaseSeconds -gt 7200) {
                throw "leaseSeconds must be between 5 and 7200"
            }
            $candidates = @(
                Get-ChildItem -LiteralPath $JobsRoot -Filter "*.json" -File | ForEach-Object {
                    Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                }
            )
            foreach ($expired in $candidates | Where-Object {
                $_.status -eq "running" -and $_.leaseExpiresUnixMs -le $now
            }) {
                $expired.status = if ($expired.attempts -lt $expired.maxAttempts) { "queued" } else { "failed" }
                $expired.leaseOwner = $null
                $expired.leaseToken = $null
                $expired.leaseExpiresUnixMs = $null
                $expired.lastError = "lease expired"
                $expired.updatedAtUnixMs = $now
                Add-Event $expired "lease_expired"
                Write-Job $expired $expired.id
            }
            $eligible = @(
                $candidates | Where-Object {
                    $_.status -eq "queued" -and
                    $_.availableAtUnixMs -le $now -and
                    (-not $engine -or $_.engine -eq $engine)
                } | Sort-Object @{Expression="priority"; Descending=$true}, createdAtUnixMs
            )
            if ($eligible.Count -eq 0) { return $null }
            $job = $eligible[0]
            $job.status = "running"
            $job.attempts += 1
            $job.leaseOwner = if ($Payload.owner) { [string]$Payload.owner } else { $env:COMPUTERNAME }
            $job.leaseToken = ([guid]::NewGuid().ToString("N"))
            $job.leaseExpiresUnixMs = $now + ($leaseSeconds * 1000)
            $job.updatedAtUnixMs = $now
            $job.lastError = $null
            Add-Event $job "claimed" @{owner = $job.leaseOwner}
            Write-Job $job $job.id
            return $job
        }
        "heartbeat" {
            $job = Read-Job ([string]$Payload.jobId)
            Assert-Lease $job ([string]$Payload.leaseToken)
            $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            $seconds = if ($null -eq $Payload.leaseSeconds) { 7200 } else { [int]$Payload.leaseSeconds }
            if ($seconds -lt 5 -or $seconds -gt 7200) {
                throw "leaseSeconds must be between 5 and 7200"
            }
            $job.leaseExpiresUnixMs = $now + ($seconds * 1000)
            $job.updatedAtUnixMs = $now
            Add-Event $job "heartbeat"
            Write-Job $job $job.id
            return $job
        }
        "succeed" {
            $job = Read-Job ([string]$Payload.jobId)
            Assert-Lease $job ([string]$Payload.leaseToken)
            $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            $job.status = "succeeded"
            $job.receipt = $Payload.receipt
            $job.leaseOwner = $null
            $job.leaseToken = $null
            $job.leaseExpiresUnixMs = $null
            $job.updatedAtUnixMs = $now
            Add-Event $job "succeeded"
            Write-Job $job $job.id
            return $job
        }
        "fail" {
            $job = Read-Job ([string]$Payload.jobId)
            Assert-Lease $job ([string]$Payload.leaseToken)
            $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            $job.lastError = ([string]$Payload.error).Substring(0, [Math]::Min(4000, ([string]$Payload.error).Length))
            if ($job.attempts -lt $job.maxAttempts) {
                $job.status = "queued"
                $job.availableAtUnixMs = $now + [int]([Math]::Min(60000, [Math]::Pow(2, $job.attempts) * 1000))
            } else {
                $job.status = "failed"
                $job.availableAtUnixMs = $now
            }
            $job.leaseOwner = $null
            $job.leaseToken = $null
            $job.leaseExpiresUnixMs = $null
            $job.updatedAtUnixMs = $now
            Add-Event $job "failed" @{retrying = $job.status -eq "queued"}
            Write-Job $job $job.id
            return $job
        }
        "cancel" {
            $job = Read-Job ([string]$Payload.jobId)
            if ($job.status -notin @("queued", "running")) {
                throw "cannot cancel terminal cluster job: $($job.status)"
            }
            $job.status = "cancelled"
            $job.leaseOwner = $null
            $job.leaseToken = $null
            $job.leaseExpiresUnixMs = $null
            $job.updatedAtUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            Add-Event $job "cancelled"
            Write-Job $job $job.id
            return $job
        }
        "get" {
            return Read-Job ([string]$Payload.jobId)
        }
        "stats" {
            $jobs = @(Get-ChildItem -LiteralPath $JobsRoot -Filter "*.json" -File | ForEach-Object {
                Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            })
            $counts = [ordered]@{}
            foreach ($status in $jobs.status | Sort-Object -Unique) {
                $counts[$status] = @($jobs | Where-Object status -eq $status).Count
            }
            return [pscustomobject][ordered]@{
                schemaVersion = "1.0"
                state = $QueueRoot
                counts = [pscustomobject]$counts
                active = @($jobs | Where-Object status -notin @("succeeded", "failed", "cancelled")).Count
            }
        }
    }
}

$Result | ConvertTo-Json -Depth 24 -Compress
