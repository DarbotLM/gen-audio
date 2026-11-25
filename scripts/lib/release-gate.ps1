param([string]$Dist = '')

function Get-ObjectPropertyValue {
    param([AllowNull()][object]$InputObject, [Parameter(Mandatory = $true)][string]$Name)
    if ($null -eq $InputObject) { return $null }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-ReleaseGateHit {
    param([Parameter(Mandatory = $true)][string]$Dist)
    if (-not (Test-Path -LiteralPath $Dist -PathType Container)) {
        return @("dist bundle is missing: $Dist")
    }
    $files = @(Get-ChildItem -LiteralPath $Dist -File -Recurse | Where-Object {
        @('.js', '.html', '.css', '.mjs', '.json') -contains $_.Extension.ToLowerInvariant()
    })
    if ($files.Count -eq 0) { return @("dist bundle has no js/html/css/json: $Dist") }

    $needles = @(
        'cube-fixture', 'spec-fixture', 'cast-sample', 'Ref only',
        'This layer is reserved', 'Sample cast', 'sample script',
        'preview, not speech', 'not remeasured', 'stand-in',
        'Load labeled example', '"state":"fixture"', '"honesty":"fixture"',
        'honesty=fixture'
    )
    $stubTerms = @('placeholder', 'fixture', 'todo stub', 'sample clip (preview)')
    $stubCardIds = @('serve-node', 'serve-gateway', 'cube-fixture', 'bench-ref', 'spec-fixture', 'cast-sample')
    $hits = New-Object System.Collections.Generic.List[string]
    foreach ($file in $files) {
        $text = [IO.File]::ReadAllText($file.FullName)
        foreach ($needle in $needles) {
            if ($text.Contains($needle)) { $hits.Add("$($file.Name): $needle") }
        }
        if ($file.Extension -ne '.json') {
            $copy = $text
            $copy = [regex]::Replace($copy, 'not a fixture|labeled fixture|fixture-tone|fixture-cube', ' ', 'IgnoreCase')
            $copy = [regex]::Replace($copy, '\bplaceholder\s*=\s*(["''`]).*?\1', ' ', 'IgnoreCase,Singleline')
            $copy = [regex]::Replace($copy, '::placeholder\b|\.placeholder\s*=|\.placeholder\b|\bsetAttribute\s*\(\s*["'']placeholder["'']|\bgetAttribute\s*\(\s*["'']placeholder["'']', ' ', 'IgnoreCase')
            $literalPattern = @'
"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`
'@
            $scannedParts = @(
                [regex]::Matches($copy, $literalPattern) | ForEach-Object { $_.Value.Substring(1, $_.Value.Length - 2) }
            )
            if ($file.Extension -eq '.html') {
                $htmlText = [regex]::Replace($copy, '<script\b[^>]*>.*?</script>', ' ', 'IgnoreCase,Singleline')
                $htmlText = [regex]::Replace($htmlText, '<style\b[^>]*>.*?</style>', ' ', 'IgnoreCase,Singleline')
                $scannedParts += [regex]::Replace($htmlText, '<[^>]+>', ' ')
            }
            $copy = ($scannedParts -join "`n").ToLowerInvariant() -replace '[-_.:;=]+', ' ' -replace '\s+', ' '
            foreach ($term in $stubTerms) {
                $pattern = switch ($term) {
                    'fixture' { '\bfixtures?\b' }
                    'todo stub' { '\btodo\s+stub\b' }
                    'sample clip (preview)' { '\bsample\s+clip\s*\(\s*preview\s*\)' }
                    default { "\b$([regex]::Escape($term))\b" }
                }
                if ($copy -match $pattern) { $hits.Add("$($file.Name): $term") }
            }
            continue
        }
        try {
            $document = $text | ConvertFrom-Json -ErrorAction Stop
        } catch {
            $hits.Add("$($file.Name): invalid json")
            continue
        }
        $assetsValue = Get-ObjectPropertyValue -InputObject $document -Name assets
        $assets = if ($null -ne $assetsValue) { @($assetsValue) } else { @() }
        foreach ($asset in $assets) {
            if (-not $asset -or $asset -isnot [pscustomobject]) {
                $hits.Add("$($file.Name): asset is not an object")
                continue
            }
            $body = Get-ObjectPropertyValue -InputObject $asset -Name body
            if ($body -isnot [pscustomobject]) { $body = [pscustomobject]@{} }
            $bodyId = Get-ObjectPropertyValue -InputObject $body -Name id
            $legacyId = Get-ObjectPropertyValue -InputObject $asset -Name legacy_id
            $cardId = if ($bodyId) { [string]$bodyId } elseif ($legacyId) { [string]$legacyId } else { '?' }
            $kind = [string](Get-ObjectPropertyValue -InputObject $body -Name kind)
            $innerBody = Get-ObjectPropertyValue -InputObject $body -Name body
            $honesty = Get-ObjectPropertyValue -InputObject $asset -Name honesty
            $honestyFixture = Get-ObjectPropertyValue -InputObject $honesty -Name fixture
            $bodyHonesty = Get-ObjectPropertyValue -InputObject $body -Name honesty
            if ($honestyFixture -eq $true -or $bodyHonesty -eq 'fixture') {
                $hits.Add("$($file.Name): $cardId`: honesty=fixture")
            }
            if ($stubCardIds -contains $cardId -or $stubCardIds -contains [string]$legacyId) {
                $hits.Add("$($file.Name): $cardId`: stub card")
            }
            if ($kind -eq 'BenchmarkCompare' -and (Get-ObjectPropertyValue -InputObject $innerBody -Name measuredHere) -ne $true) {
                $hits.Add("$($file.Name): $cardId`: BenchmarkCompare.measuredHere is not true")
            }
            if ($kind -eq 'ServeHealth') {
                $hostName = [string](Get-ObjectPropertyValue -InputObject $innerBody -Name host)
                if (-not $hostName -or $hostName -match '^<[^>]+>$') { $hits.Add("$($file.Name): $cardId`: ServeHealth host is a placeholder") }
                if ((Get-ObjectPropertyValue -InputObject $innerBody -Name probed) -ne $true) { $hits.Add("$($file.Name): $cardId`: ServeHealth has no probe result") }
            }
            if (($asset | ConvertTo-Json -Depth 64 -Compress) -match 'not remeasured|stand-in|\bstand in\b') {
                $hits.Add("$($file.Name): $cardId`: stub phrase")
            }
        }
    }
    return @($hits | Sort-Object -Unique)
}

if ($MyInvocation.InvocationName -ne '.') {
    if (-not $Dist) { throw 'Dist is required.' }
    $hits = @(Get-ReleaseGateHit -Dist $Dist)
    $hits | Write-Output
    if ($hits.Count -gt 0) { exit 1 }
    Write-Output '# pass release gate'
}
