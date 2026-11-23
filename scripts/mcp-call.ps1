<#
.SYNOPSIS
Call one Gen-Audio MCP tool over HTTP JSON-RPC (tools/call) and print the result.

.DESCRIPTION
Finds the server the same way the desktop app binds it, then checks that the
port really is Gen-Audio (initialize, serverInfo.name = gen-audio):
  1. -Port, when given
  2. GEN_AUDIO_MCP_ADDR (host:port), the desktop app's preferred address
  3. the checkout's MCP port from scripts/runtime.json or its descriptor
  4. Windows only: loopback ports a gen-audio / gen-audio-mcp process is
     listening on (the app falls back through its configured loopback range)
Prints `MCP addr=<host:port> tool=<name>` and then the JSON-RPC result.
Exits non-zero on a JSON-RPC error or a tool result with isError true.

-OutFile <path> writes only the JSON-RPC response body to <path> (UTF-8, no
BOM, the bytes as received), also when the response is a JSON-RPC error, so
the evidence file is machine-checkable (ConvertFrom-Json, python json.load).
With -OutFile the `MCP addr=... tool=...` header and a final `exit=<N>` line go
to stderr and nothing goes to stdout; Write-Error stays on the error stream and
the exit code is the same as without it. Without -OutFile the header and the
body go to stdout as before.
Works on Windows PowerShell 5.1 and PowerShell 7.

.EXAMPLE
pwsh -NoProfile -File scripts/mcp-call.ps1 -Tool ui_navigate -ArgsJson '{"slide":"slide:library"}'

.EXAMPLE
pwsh -NoProfile -File scripts/mcp-call.ps1 -Tool viewport_get -OutFile artifacts/viewport.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Tool,
    [string]$ArgsJson = '{}',
    [int]$Port = 0,
    [int]$TimeoutSec = 15,
    # Write only the JSON-RPC response body here (UTF-8, no BOM); header and exit=<N> go to stderr.
    [string]$OutFile = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
# The response is UTF-8 JSON for a program to parse. Redirected stdout otherwise
# uses the console's OEM code page and turns non-ASCII into '?'.
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) }
catch { Write-Verbose "Console output encoding unchanged: $($_.Exception.Message)" }
. (Join-Path $PSScriptRoot 'lib\devenv.ps1')
. (Join-Path $PSScriptRoot 'lib\runtime-config.ps1')
$runtimeConfig = Read-GenAudioRuntimeConfig -RepoRoot (Split-Path -Parent $PSScriptRoot)

function Test-McpLoopbackAddress {
    param([Parameter(Mandatory = $true)][string]$Address)
    try {
        $uri = [Uri]::new("http://$Address")
        $uriHost = $uri.Host.Trim('[', ']')
        if ($uriHost -eq 'localhost') { return $true }
        $ip = $null
        return ([Net.IPAddress]::TryParse($uriHost, [ref]$ip) -and [Net.IPAddress]::IsLoopback($ip))
    } catch {
        return $false
    }
}

function Test-McpCredential {
    param([string]$Token)
    return (-not $Token) -or ($Token -match '^[0-9a-fA-F]{64}$')
}

function Invoke-JsonRpc {
    param(
        [Parameter(Mandatory = $true)][string]$Address,
        [Parameter(Mandatory = $true)][string]$Body,
        [string]$Token = '',
        [int]$Timeout = 5
    )
    $headers = @{}
    if ($Token) { $headers['Authorization'] = "Bearer $Token" }
    $response = Invoke-WebRequest -Uri "http://$Address/mcp" -Method POST -ContentType 'application/json' -Headers $headers -Body ([System.Text.Encoding]::UTF8.GetBytes($Body)) -UseBasicParsing -TimeoutSec $Timeout
    $text = $response.Content
    if ($text -is [byte[]]) { $text = [System.Text.Encoding]::UTF8.GetString($text) }
    return $text
}

function Test-GenAudioMcp {
    [OutputType([bool])]
    param([Parameter(Mandatory = $true)][string]$Address, [string]$Token = '')
    $init = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"mcp-call","version":"0.1.0"}}}'
    try {
        $reply = (Invoke-JsonRpc -Address $Address -Token $Token -Body $init -Timeout 3) | ConvertFrom-Json
        return ($reply.result.serverInfo.name -eq 'gen-audio')
    } catch {
        Write-Verbose "no Gen-Audio MCP at ${Address}: $($_.Exception.Message)"
        return $false
    }
}

try {
    $arguments = $ArgsJson | ConvertFrom-Json
} catch {
    throw "-ArgsJson is not valid JSON: $ArgsJson"
}
if ($null -eq $arguments -or $arguments -is [array] -or $arguments -is [string] -or $arguments -is [ValueType]) {
    throw "-ArgsJson must be a JSON object, got: $ArgsJson"
}

$candidates = New-Object System.Collections.Generic.List[string]
if ($Port -gt 0) {
    $candidates.Add("127.0.0.1:$Port")
} else {
    if ($env:GEN_AUDIO_MCP_ADDR) {
        if (-not (Test-McpLoopbackAddress -Address $env:GEN_AUDIO_MCP_ADDR)) {
            throw "GEN_AUDIO_MCP_ADDR must name a loopback MCP listener."
        }
        $candidates.Add($env:GEN_AUDIO_MCP_ADDR)
    }
    $candidates.Add("127.0.0.1:$($runtimeConfig.ports.Mcp)")
    if ((Test-IsWindowsHost) -and (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        $owners = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like 'gen-audio*' } | ForEach-Object { $_.Id })
        if ($owners.Count -gt 0) {
            $listening = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                    Where-Object { $owners -contains $_.OwningProcess -and ($_.LocalAddress -eq '127.0.0.1' -or $_.LocalAddress -eq '::1') })
            foreach ($socket in $listening) {
                $address = if ($socket.LocalAddress -eq '::1') { "[::1]:$($socket.LocalPort)" } else { "127.0.0.1:$($socket.LocalPort)" }
                if (-not $candidates.Contains($address)) { $candidates.Add($address) }
            }
        }
    }
}

$descriptorPath = if ($env:GEN_AUDIO_MCP_ADDR_FILE) {
    $env:GEN_AUDIO_MCP_ADDR_FILE
} else {
    $runtimeConfig.mcpAddressFile
}
$descriptorToken = ''
$descriptorAddr = ''
if (Test-Path -LiteralPath $descriptorPath -PathType Leaf) {
    try {
        $descriptorLines = [IO.File]::ReadAllLines($descriptorPath)
        if ($descriptorLines.Count -ge 3) {
            $nextDescriptorAddr = $descriptorLines[0].Trim()
            $nextDescriptorToken = $descriptorLines[2].Trim()
            if (-not (Test-McpLoopbackAddress -Address $nextDescriptorAddr)) {
                throw "MCP descriptor address must be loopback."
            }
            if (-not $nextDescriptorToken -or $nextDescriptorToken -notmatch '^[0-9a-fA-F]{64}$') {
                throw "MCP descriptor credential must be nonempty and 64 hexadecimal digits."
            }
            $descriptorAddr = $nextDescriptorAddr
            $descriptorToken = $nextDescriptorToken
            if ($Port -eq 0 -and $descriptorAddr -and -not $candidates.Contains($descriptorAddr)) {
                $candidates.Insert(0, $descriptorAddr)
            }
        }
    } catch {
        Write-Verbose "invalid MCP descriptor ${descriptorPath}: $($_.Exception.Message)"
    }
}
$environmentToken = [Environment]::GetEnvironmentVariable('GEN_AUDIO_MCP_TOKEN', 'Process')
if ($null -eq $environmentToken) { $environmentToken = '' }
if (-not (Test-McpCredential -Token $environmentToken)) {
    throw 'GEN_AUDIO_MCP_TOKEN must be 64 hexadecimal digits.'
}

$addr = $null
$token = ''
foreach ($candidate in $candidates) {
    $candidateToken = ''
    if ($environmentToken) {
        $candidateToken = $environmentToken
    } elseif ($candidate -eq $descriptorAddr -and $descriptorToken) {
        $candidateToken = $descriptorToken
    }
    if (Test-GenAudioMcp -Address $candidate -Token $candidateToken) {
        $addr = $candidate
        $token = $candidateToken
        break
    }
}
if (-not $addr) { throw "no Gen-Audio MCP server answered initialize on: $($candidates -join ', ')" }

$request = @{ jsonrpc = '2.0'; id = 2; method = 'tools/call'; params = @{ name = $Tool; arguments = $arguments } } | ConvertTo-Json -Depth 32 -Compress
$text = Invoke-JsonRpc -Address $addr -Token $token -Body $request -Timeout $TimeoutSec
if ($OutFile) {
    # The file holds the body and nothing else; the header goes to stderr.
    $outPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutFile)
    [System.IO.File]::WriteAllText($outPath, $text, (New-Object System.Text.UTF8Encoding($false)))
    [Console]::Error.WriteLine("MCP addr=$addr tool=$Tool")
} else {
    Write-Output "MCP addr=$addr tool=$Tool"
    Write-Output $text
}
$code = 0
$reply = $text | ConvertFrom-Json
if ($reply.PSObject.Properties.Name -contains 'error') {
    Write-Error "JSON-RPC error $($reply.error.code): $($reply.error.message)" -ErrorAction Continue
    $code = 1
} elseif ($reply.result.PSObject.Properties.Name -contains 'isError' -and $reply.result.isError) {
    Write-Error "tool $Tool returned isError" -ErrorAction Continue
    $code = 1
}
if ($OutFile) { [Console]::Error.WriteLine("exit=$code") }
exit $code
