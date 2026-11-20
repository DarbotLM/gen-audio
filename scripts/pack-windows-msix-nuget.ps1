<#
.SYNOPSIS
Pack the Gen-Audio release binaries as an MSIX and a tools NuGet package.

.DESCRIPTION
Runs after scripts\build-tauri-windows.ps1 -Mode Full, so Windows ships the
same gen-audio.exe and gen-audio-mcp sidecar as NSIS, MSI, MSIX and NuGet.
Writes target\release\bundle\msix\Gen-Audio_<version>_x64.msix and
target\release\bundle\nuget\DarbotLabs.GenAudio.<version>.nupkg, then prints
one PACKAGE line per artifact with its sha256.

Both packages are unsigned unless -CertificateThumbprint names a code-signing
certificate already in CurrentUser\My or LocalMachine\My; the script never
creates or imports certificates. An unsigned MSIX installs with
Add-AppxPackage -AllowUnsigned in Developer Mode.

.EXAMPLE
pwsh -NoProfile -File scripts/pack-windows-msix-nuget.ps1
#>
[CmdletBinding()]
param(
    [string]$CertificateThumbprint,
    [string]$TimestampUrl = 'http://timestamp.digicert.com',
    [ValidateRange(0, 65535)]
    [int]$MsixRevision = 0
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$root = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $root 'target\release'
$bundleDir = Join-Path $releaseDir 'bundle'
$conf = Get-Content -Raw -LiteralPath (Join-Path $root 'apps\desktop\src-tauri\tauri.conf.json') | ConvertFrom-Json
$version = [string]$conf.version
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "tauri.conf.json version '$version' is not major.minor.patch" }
$binaries = @('gen-audio.exe', 'gen-audio-mcp.exe', 'gen-audio-acp.exe') | ForEach-Object {
    $p = Join-Path $releaseDir $_
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "$p is missing; run scripts\build-tauri-windows.ps1 -Mode Full first" }
    Get-Item -LiteralPath $p
}
$appExe = $binaries[0]

function Get-SdkTool([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    $found = @(Get-ChildItem -Path (Join-Path $kits "*\x64\$Name") -ErrorAction SilentlyContinue | Sort-Object { [version]$_.Directory.Parent.Name })
    if ($found.Count -eq 0) { return $null }
    return $found[-1].FullName
}

function Invoke-Native([string]$File, [string[]]$Arguments) {
    & $File @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "$([IO.Path]::GetFileName($File)) failed with exit code $LASTEXITCODE" }
}

function Write-Package([string]$Kind, [string]$Path, [string]$Signed) {
    $item = Get-Item -LiteralPath $Path
    $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Output "PACKAGE kind=$Kind path=$($item.FullName) size=$($item.Length) sha256=$hash signed=$Signed"
}

$cert = $null
if ($CertificateThumbprint) {
    $cert = Get-ChildItem -Path Cert:\CurrentUser\My, Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
        Where-Object Thumbprint -eq $CertificateThumbprint | Select-Object -First 1
    if (-not $cert) { throw "Certificate $CertificateThumbprint is not in CurrentUser\My or LocalMachine\My" }
}
$signed = if ($cert) { 'yes' } else { 'no' }

# ---------------------------------------------------------------- MSIX
$makeappx = Get-SdkTool 'makeappx.exe'
if (-not $makeappx) { throw 'makeappx.exe was not found; install the Windows 10/11 SDK' }
$msixDir = Join-Path $bundleDir 'msix'
$layout = Join-Path $msixDir 'layout'
if (Test-Path -LiteralPath $msixDir) { Remove-Item -LiteralPath $msixDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $layout 'Assets') | Out-Null
foreach ($bin in $binaries) { Copy-Item -LiteralPath $bin.FullName -Destination (Join-Path $layout $bin.Name) }
Add-Type -AssemblyName System.Drawing
$iconPng = Join-Path $root 'apps\desktop\src-tauri\icons\icon.png'
$src = [System.Drawing.Image]::FromFile($iconPng)
try {
    foreach ($spec in @(@('StoreLogo.png', 50), @('Square44x44Logo.png', 44), @('Square150x150Logo.png', 150))) {
        $bmp = New-Object System.Drawing.Bitmap([int]$spec[1], [int]$spec[1])
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.DrawImage($src, 0, 0, [int]$spec[1], [int]$spec[1])
        $g.Dispose()
        $bmp.Save((Join-Path $layout "Assets\$($spec[0])"), [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
    }
} finally { $src.Dispose() }
# Unsigned packages use the unsigned-namespace publisher so Add-AppxPackage -AllowUnsigned accepts them.
$publisher = if ($cert) { $cert.Subject } else { 'CN=Darbot Labs, OID.2.25.311729368913984317654407730594956997722=1' }
$esc = { param($s) [Security.SecurityElement]::Escape([string]$s) }
$manifest = @"
<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
         xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
         IgnorableNamespaces="uap rescap">
  <Identity Name="DarbotLabs.GenAudio" Publisher="$(& $esc $publisher)" Version="$version.$MsixRevision" ProcessorArchitecture="x64" />
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
$msix = Join-Path $msixDir "$($conf.productName)_$($version)_x64.msix"
Invoke-Native $makeappx @('pack', '/d', $layout, '/p', $msix, '/o')
Remove-Item -LiteralPath $layout -Recurse -Force
$signtool = $null
if ($cert) {
    $signtool = Get-SdkTool 'signtool.exe'
    if (-not $signtool) { throw 'signtool.exe was not found; install the Windows 10/11 SDK' }
    Invoke-Native $signtool @('sign', '/fd', 'SHA256', '/sha1', $CertificateThumbprint, '/tr', $TimestampUrl, '/td', 'SHA256', $msix)
}
Write-Package 'msix' $msix $signed

# ---------------------------------------------------------------- NuGet
# A tools-only package (nuget install / Chocolatey style) with the same binaries.
$id = 'DarbotLabs.GenAudio'
$nugetDir = Join-Path $bundleDir 'nuget'
if (Test-Path -LiteralPath $nugetDir) { Remove-Item -LiteralPath $nugetDir -Recurse -Force }
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
    <description>$(& $esc 'Darbot Gen-Audio desktop for Windows x64. tools/ holds gen-audio.exe, the gen-audio-mcp sidecar and the gen-audio-acp agent; the MSIX carries the same binaries.')</description>
    <projectUrl>https://github.com/darbotlabs/gen-audio</projectUrl>
    <license type="expression">MIT</license>
    <requireLicenseAcceptance>false</requireLicenseAcceptance>
    <tags>gen-audio tts tauri desktop mcp windows</tags>
  </metadata>
</package>
"@
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::Open($nupkg, [IO.Compression.ZipArchiveMode]::Create)
try {
    $write = {
        param([string]$Name, [string]$Text)
        $entry = $zip.CreateEntry($Name, [IO.Compression.CompressionLevel]::Optimal)
        $writer = New-Object IO.StreamWriter($entry.Open(), (New-Object Text.UTF8Encoding($false)))
        try { $writer.Write($Text) } finally { $writer.Dispose() }
    }
    & $write "$id.nuspec" $nuspec
    foreach ($bin in $binaries) {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $bin.FullName, "tools/$($bin.Name)", [IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
    $coreProps = 'package/services/metadata/core-properties/' + [guid]::NewGuid().ToString('N') + '.psmdcp'
    & $write '_rels/.rels' @"
<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.microsoft.com/packaging/2010/07/manifest" Target="/$id.nuspec" Id="R1" />
  <Relationship Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="/$coreProps" Id="R2" />
</Relationships>
"@
    & $write $coreProps @"
<?xml version="1.0" encoding="utf-8"?>
<coreProperties xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns="http://schemas.openxmlformats.org/package/2006/metadata/core-properties">
  <dc:creator>Darbot Labs</dc:creator>
  <dc:description>Darbot Gen-Audio desktop</dc:description>
  <dc:identifier>$id</dc:identifier>
  <version>$version</version>
  <keywords>gen-audio tts tauri desktop mcp windows</keywords>
  <lastModifiedBy>scripts/pack-windows-msix-nuget.ps1</lastModifiedBy>
</coreProperties>
"@
    & $write '[Content_Types].xml' @"
<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />
  <Default Extension="nuspec" ContentType="application/octet" />
  <Default Extension="exe" ContentType="application/octet" />
  <Default Extension="psmdcp" ContentType="application/vnd.openxmlformats-package.core-properties+xml" />
</Types>
"@
} finally { $zip.Dispose() }
if ($cert) {
    $dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
    if (-not $dotnet) { throw 'dotnet is required to sign the NuGet package' }
    Invoke-Native $dotnet.Source @('nuget', 'sign', $nupkg, '--certificate-fingerprint', $CertificateThumbprint, '--timestamper', $TimestampUrl, '--overwrite')
}
Write-Package 'nupkg' $nupkg $signed
