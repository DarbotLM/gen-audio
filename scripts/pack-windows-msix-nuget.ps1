<#
.SYNOPSIS
Pack verified Gen-Audio release binaries as MSIX and a tools NuGet package.

.DESCRIPTION
Runs after scripts\build-tauri-windows.ps1 -Mode Full, so the MSIX and NuGet
package contain the same gen-audio.exe, gen-audio-mcp.exe, gen-audio-acp.exe,
and wingen.audio.exe that passed the build's provenance gates. The build passes
their sha256 values to make that invariant explicit.

Packages are unsigned by default. When -CertificateThumbprint names an
existing code-signing certificate with a private key in CurrentUser\My or
LocalMachine\My, the script signs both packages. It never creates or imports a
certificate.

.EXAMPLE
pwsh -NoProfile -File scripts\pack-windows-msix-nuget.ps1
#>
[CmdletBinding()]
param(
    [string]$CertificateThumbprint,
    [string]$TimestampUrl = 'https://timestamp.digicert.com',
    [ValidateRange(0, 65535)]
    [int]$MsixRevision = 0,
    [string]$ExpectedExeSha256,
    [string]$ExpectedMcpSha256,
    [string]$ExpectedAcpSha256,
    [string]$ExpectedWingenSha256
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'lib\devenv.ps1')

if (-not (Test-IsWindowsHost)) {
    throw 'pack-windows-msix-nuget.ps1 packages Windows artifacts; run it on Windows.'
}

$root = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $root 'target\release'
$bundleDir = Join-Path $releaseDir 'bundle'
$confPath = Join-Path $root 'apps\desktop\src-tauri\tauri.conf.json'
$conf = Get-Content -LiteralPath $confPath -Raw | ConvertFrom-Json
$version = [string]$conf.version
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "tauri.conf.json version '$version' is not major.minor.patch"
}

function Get-SdkTool([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $programFilesX86 = [Environment]::GetFolderPath('ProgramFilesX86')
    if ([string]::IsNullOrWhiteSpace($programFilesX86)) { return $null }
    $kits = Join-Path $programFilesX86 'Windows Kits\10\bin'
    $found = @(Get-ChildItem -Path (Join-Path $kits "*\x64\$Name") -ErrorAction SilentlyContinue |
            Sort-Object { [version]$_.Directory.Parent.Name })
    if ($found.Count -eq 0) { return $null }
    return $found[-1].FullName
}

function Invoke-Native([string]$File, [string[]]$Arguments) {
    & $File @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "$([IO.Path]::GetFileName($File)) failed with exit code $LASTEXITCODE"
    }
}

function Get-StreamSha256([IO.Stream]$Stream) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Assert-ArchivePayload(
    [string]$Kind,
    [string]$Path,
    [string]$Prefix,
    [object[]]$Binaries
) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        foreach ($binary in $Binaries) {
            $memberName = "$Prefix$($binary.Name)"
            $entries = @($archive.Entries | Where-Object {
                    $_.FullName.Replace('\', '/') -ieq $memberName
                })
            if ($entries.Count -ne 1) {
                throw "$Kind package $Path must contain exactly one $memberName payload; found $($entries.Count)"
            }
            $stream = $entries[0].Open()
            try {
                $memberHash = Get-StreamSha256 -Stream $stream
            } finally {
                $stream.Dispose()
            }
            if ($memberHash -ne $binary.Sha256) {
                throw "$Kind payload $memberName has sha256 $memberHash, expected $($binary.Sha256)"
            }
        }
    } finally {
        $archive.Dispose()
    }
}

function Write-Package([string]$Kind, [string]$Path, [string]$Signed) {
    $item = Get-Item -LiteralPath $Path
    $hash = Get-Sha256 -LiteralPath $item.FullName
    Write-Output "PACKAGE kind=$Kind path=$($item.FullName) size=$($item.Length) sha256=$hash signed=$Signed"
}

$payloadSpecs = @(
    [pscustomobject]@{ Name = 'gen-audio.exe'; Expected = $ExpectedExeSha256 },
    [pscustomobject]@{ Name = 'gen-audio-mcp.exe'; Expected = $ExpectedMcpSha256 },
    [pscustomobject]@{ Name = 'gen-audio-acp.exe'; Expected = $ExpectedAcpSha256 },
    [pscustomobject]@{ Name = 'wingen.audio.exe'; Expected = $ExpectedWingenSha256 }
)
$binaries = @(
    foreach ($spec in $payloadSpecs) {
        $path = Join-Path $releaseDir $spec.Name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "$path is missing; run scripts\build-tauri-windows.ps1 -Mode Full first"
        }
        $item = Get-Item -LiteralPath $path
        $hash = Get-Sha256 -LiteralPath $item.FullName
        if ($spec.Expected) {
            if ($spec.Expected -notmatch '^[0-9a-fA-F]{64}$') {
                throw "Expected sha256 for $($spec.Name) is not a 64-digit hexadecimal value"
            }
            if ($hash -ne $spec.Expected.ToLowerInvariant()) {
                throw "$($item.FullName) has sha256 $hash; the verified Full build expected $($spec.Expected.ToLowerInvariant())"
            }
        }
        [pscustomobject]@{ Name = $item.Name; Path = $item.FullName; Length = $item.Length; Sha256 = $hash }
    }
)
$appExe = $binaries[0]

$certificateStore = $null
$certificate = $null
if ($CertificateThumbprint) {
    $thumbprint = $CertificateThumbprint -replace '\s', ''
    foreach ($store in 'CurrentUser', 'LocalMachine') {
        $candidate = Get-ChildItem -Path "Cert:\$store\My" -ErrorAction SilentlyContinue |
            Where-Object { $_.Thumbprint -eq $thumbprint } | Select-Object -First 1
        if (-not $candidate) { continue }
        $certificate = $candidate
        $certificateStore = $store
        break
    }
    if (-not $certificate) {
        throw "Certificate $CertificateThumbprint is not in CurrentUser\My or LocalMachine\My"
    }
    if (-not $certificate.HasPrivateKey) {
        throw "Certificate $($certificate.Thumbprint) has no private key and cannot sign the packages"
    }
    if ($certificate.NotAfter.ToUniversalTime() -le (Get-Date).ToUniversalTime()) {
        throw "Certificate $($certificate.Thumbprint) expired $($certificate.NotAfter)"
    }
    $ekuExtension = $certificate.Extensions |
        Where-Object { $_.Oid.Value -eq '2.5.29.37' } | Select-Object -First 1
    if ($ekuExtension -and @($ekuExtension.EnhancedKeyUsages |
                Where-Object { $_.Value -eq '1.3.6.1.5.5.7.3.3' }).Count -eq 0) {
        throw "Certificate $($certificate.Thumbprint) is not valid for code signing"
    }
}
$signed = if ($certificate) { 'yes' } else { 'no' }

$msixDir = Join-Path $bundleDir 'msix'
$layout = Join-Path $msixDir 'layout'
if (Test-Path -LiteralPath $msixDir) {
    Remove-Item -LiteralPath $msixDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $layout 'Assets') | Out-Null
foreach ($binary in $binaries) {
    $copy = Join-Path $layout $binary.Name
    Copy-Item -LiteralPath $binary.Path -Destination $copy
    if ((Get-Sha256 -LiteralPath $copy) -ne $binary.Sha256) {
        throw "MSIX layout copy $copy changed the verified $($binary.Name) payload"
    }
}

Add-Type -AssemblyName System.Drawing
$iconPng = Join-Path $root 'apps\desktop\src-tauri\icons\icon.png'
if (-not (Test-Path -LiteralPath $iconPng -PathType Leaf)) {
    throw "MSIX source icon is missing: $iconPng"
}
$sourceIcon = [System.Drawing.Image]::FromFile($iconPng)
try {
    foreach ($spec in @(@('StoreLogo.png', 50), @('Square44x44Logo.png', 44), @('Square150x150Logo.png', 150))) {
        $bitmap = New-Object System.Drawing.Bitmap([int]$spec[1], [int]$spec[1])
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.DrawImage($sourceIcon, 0, 0, [int]$spec[1], [int]$spec[1])
            $bitmap.Save((Join-Path $layout "Assets\$($spec[0])"), [System.Drawing.Imaging.ImageFormat]::Png)
        } finally {
            $graphics.Dispose()
            $bitmap.Dispose()
        }
    }
} finally {
    $sourceIcon.Dispose()
}

$publisher = if ($certificate) { $certificate.Subject } else {
    'CN=Darbot Labs, OID.2.25.311729368913984317654407730594956997722=1'
}
$escapeXml = { param($Text) [Security.SecurityElement]::Escape([string]$Text) }
$manifest = @"
<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
         xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
         IgnorableNamespaces="uap rescap">
  <Identity Name="DarbotLabs.GenAudio" Publisher="$(& $escapeXml $publisher)" Version="$version.$MsixRevision" ProcessorArchitecture="x64" />
  <Properties>
    <DisplayName>Gen-Audio</DisplayName>
    <PublisherDisplayName>Darbot Labs</PublisherDisplayName>
    <Logo>Assets\StoreLogo.png</Logo>
  </Properties>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.19041.0" MaxVersionTested="10.0.26100.0" />
  </Dependencies>
  <Resources>
    <Resource Language="en-us" />
  </Resources>
  <Applications>
    <Application Id="GenAudio" Executable="$($appExe.Name)" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="Gen-Audio" Description="Darbot Gen-Audio desktop" BackgroundColor="transparent"
                          Square150x150Logo="Assets\Square150x150Logo.png" Square44x44Logo="Assets\Square44x44Logo.png" />
    </Application>
  </Applications>
  <Capabilities>
    <rescap:Capability Name="runFullTrust" />
  </Capabilities>
</Package>
"@
Set-Content -LiteralPath (Join-Path $layout 'AppxManifest.xml') -Value $manifest -Encoding UTF8

$makeappx = Get-SdkTool 'makeappx.exe'
if (-not $makeappx) {
    throw 'makeappx.exe was not found; install the Windows 10/11 SDK'
}
$msix = Join-Path $msixDir "$($conf.productName)_$($version)_x64.msix"
Invoke-Native $makeappx @('pack', '/d', $layout, '/p', $msix, '/o')
Remove-Item -LiteralPath $layout -Recurse -Force
if ($certificate) {
    $signtool = Get-SdkTool 'signtool.exe'
    if (-not $signtool) {
        throw 'signtool.exe was not found; install the Windows 10/11 SDK'
    }
    $signArgs = @('sign', '/fd', 'SHA256', '/sha1', $certificate.Thumbprint, '/tr', $TimestampUrl, '/td', 'SHA256')
    if ($certificateStore -eq 'LocalMachine') { $signArgs += '/sm' }
    $signArgs += $msix
    Invoke-Native $signtool $signArgs
}
Assert-ArchivePayload -Kind 'msix' -Path $msix -Prefix '' -Binaries $binaries
Write-Package 'msix' $msix $signed

$id = 'DarbotLabs.GenAudio'
$nugetDir = Join-Path $bundleDir 'nuget'
if (Test-Path -LiteralPath $nugetDir) {
    Remove-Item -LiteralPath $nugetDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $nugetDir | Out-Null
$nupkg = Join-Path $nugetDir "$id.$version.nupkg"
$nuspec = @"
<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">
  <metadata>
    <id>$id</id>
    <version>$version</version>
    <title>Gen-Audio</title>
    <authors>Darbot Labs</authors>
    <description>$(& $escapeXml 'Darbot Gen-Audio desktop for Windows x64. tools/ contains gen-audio.exe, the MCP and ACP sidecars, and the self-contained wingen.audio Windows execution host; the MSIX carries the same verified binaries.')</description>
    <projectUrl>https://github.com/darbotlabs/gen-audio</projectUrl>
    <license type="expression">MIT</license>
    <requireLicenseAcceptance>false</requireLicenseAcceptance>
    <tags>gen-audio tts tauri desktop mcp acp windows</tags>
  </metadata>
</package>
"@

Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::Open($nupkg, [IO.Compression.ZipArchiveMode]::Create)
try {
    $writeEntry = {
        param([string]$Name, [string]$Text)
        $entry = $zip.CreateEntry($Name, [IO.Compression.CompressionLevel]::Optimal)
        $writer = New-Object IO.StreamWriter($entry.Open(), (New-Object Text.UTF8Encoding($false)))
        try {
            $writer.Write($Text)
        } finally {
            $writer.Dispose()
        }
    }
    & $writeEntry "$id.nuspec" $nuspec
    foreach ($binary in $binaries) {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip,
            $binary.Path,
            "tools/$($binary.Name)",
            [IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
    $coreProps = 'package/services/metadata/core-properties/' + [guid]::NewGuid().ToString('N') + '.psmdcp'
    & $writeEntry '_rels/.rels' @"
<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.microsoft.com/packaging/2010/07/manifest" Target="/$id.nuspec" Id="R1" />
  <Relationship Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="/$coreProps" Id="R2" />
</Relationships>
"@
    & $writeEntry $coreProps @"
<?xml version="1.0" encoding="utf-8"?>
<coreProperties xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns="http://schemas.openxmlformats.org/package/2006/metadata/core-properties">
  <dc:creator>Darbot Labs</dc:creator>
  <dc:description>Darbot Gen-Audio desktop</dc:description>
  <dc:identifier>$id</dc:identifier>
  <version>$version</version>
  <keywords>gen-audio tts tauri desktop mcp acp windows</keywords>
  <lastModifiedBy>scripts/pack-windows-msix-nuget.ps1</lastModifiedBy>
</coreProperties>
"@
    & $writeEntry '[Content_Types].xml' @"
<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />
  <Default Extension="nuspec" ContentType="application/octet" />
  <Default Extension="exe" ContentType="application/octet" />
  <Default Extension="psmdcp" ContentType="application/vnd.openxmlformats-package.core-properties+xml" />
</Types>
"@
} finally {
    $zip.Dispose()
}
if ($certificate) {
    $dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
    if (-not $dotnet) {
        throw 'dotnet is required to sign the NuGet package'
    }
    $nugetSignArgs = @(
        'nuget', 'sign', $nupkg,
        '--certificate-fingerprint', $certificate.Thumbprint,
        '--certificate-store-name', 'My',
        '--certificate-store-location', $certificateStore,
        '--timestamper', $TimestampUrl,
        '--overwrite'
    )
    Invoke-Native $dotnet.Source $nugetSignArgs
}
Assert-ArchivePayload -Kind 'nupkg' -Path $nupkg -Prefix 'tools/' -Binaries $binaries
Write-Package 'nupkg' $nupkg $signed
