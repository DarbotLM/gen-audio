BeforeAll {
    $script:RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $script:McpCall = Join-Path $script:RepoRoot 'scripts\mcp-call.ps1'
    $script:Shell = (Get-Process -Id $PID).Path
    $script:Init = @{ jsonrpc = '2.0'; id = 1; result = @{ protocolVersion = '2025-03-26'; serverInfo = @{ name = 'gen-audio'; version = 't' } } }
    $script:Ok = @{ jsonrpc = '2.0'; id = 2; result = @{ content = @(@{ type = 'text'; text = "glyph $([char]0x2803)$([char]0x2817) ok" }); isError = $false } }
    $script:ErrorResponse = @{ jsonrpc = '2.0'; id = 2; error = @{ code = -32602; message = "unknown tool $([char]0x2803)" } }

    function Start-TestJsonRpcServer {
        param(
            [hashtable]$ToolsCall,
            [string]$Token
        )
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
        $ps = [powershell]::Create()
        [void]$ps.AddScript({
            param($Listener, $Initialize, $Tools, $RequiredToken)
            function Read-Request {
                param([Net.Sockets.TcpClient]$Client)
                $stream = $Client.GetStream()
                $header = [IO.MemoryStream]::new()
                $state = 0
                while ($true) {
                    $value = $stream.ReadByte()
                    if ($value -lt 0) { break }
                    $header.WriteByte([byte]$value)
                    $state = if ($state -eq 0 -and $value -eq 13) { 1 }
                        elseif ($state -eq 1 -and $value -eq 10) { 2 }
                        elseif ($state -eq 2 -and $value -eq 13) { 3 }
                        elseif ($state -eq 3 -and $value -eq 10) { break; 0 }
                        else { 0 }
                }
                $headerText = [Text.Encoding]::ASCII.GetString($header.ToArray())
                $lengthMatch = [regex]::Match($headerText, '(?im)^Content-Length:\s*(\d+)')
                $length = [int]$lengthMatch.Groups[1].Value
                $body = [byte[]]::new($length)
                $read = 0
                while ($read -lt $length) {
                    $count = $stream.Read($body, $read, $length - $read)
                    if ($count -le 0) { break }
                    $read += $count
                }
                return @{
                    Stream = $stream
                    Header = $headerText
                    Body = ([Text.Encoding]::UTF8.GetString($body) | ConvertFrom-Json)
                }
            }
            try {
                for ($index = 0; $index -lt 2; $index++) {
                    $client = $Listener.AcceptTcpClient()
                    try {
                        $request = Read-Request -Client $client
                        $authorized = -not $RequiredToken -or $request.Header -match "(?im)^Authorization:\s*Bearer $([regex]::Escape($RequiredToken))\s*$"
                        if (-not $authorized) {
                            $status = '401 Unauthorized'
                            $payload = '{"error":"unauthorized"}'
                        } else {
                            $status = '200 OK'
                            $value = if ($request.Body.method -eq 'initialize') { $Initialize } else { $Tools }
                            $payload = $value | ConvertTo-Json -Compress -Depth 32
                        }
                        $bodyBytes = [Text.Encoding]::UTF8.GetBytes($payload)
                        $head = "HTTP/1.1 $status`r`nContent-Type: application/json; charset=utf-8`r`nContent-Length: $($bodyBytes.Length)`r`nConnection: close`r`n`r`n"
                        $headBytes = [Text.Encoding]::ASCII.GetBytes($head)
                        $request.Stream.Write($headBytes, 0, $headBytes.Length)
                        $request.Stream.Write($bodyBytes, 0, $bodyBytes.Length)
                        $request.Stream.Flush()
                    } finally {
                        $client.Dispose()
                    }
                }
            } catch {
                if ($Listener.Active) { throw }
            }
        })
        [void]$ps.AddArgument($listener).AddArgument($script:Init).AddArgument($ToolsCall).AddArgument($Token)
        $handle = $ps.BeginInvoke()
        return [pscustomobject]@{ Port = $port; Listener = $listener; PowerShell = $ps; Handle = $handle }
    }

    function Stop-TestJsonRpcServer {
        param($Server)
        try {
            $Server.Listener.Stop()
            if ($Server.Handle.IsCompleted) {
                [void]$Server.PowerShell.EndInvoke($Server.Handle)
            } else {
                [void]$Server.PowerShell.Stop()
            }
        } finally {
            $Server.PowerShell.Dispose()
        }
    }

    function Invoke-McpCall {
        param(
            [int]$Port,
            [string[]]$Extra = @(),
            [hashtable]$Environment = @{}
        )
        $arguments = @('-NoProfile', '-NonInteractive', '-File', $script:McpCall, '-Tool', 'viewport_get')
        if ($Port -gt 0) { $arguments += @('-Port', "$Port") }
        $arguments += $Extra
        $saved = @{}
        foreach ($name in $Environment.Keys) {
            $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, [string]$Environment[$name], 'Process')
        }
        try {
            $previousErrorAction = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            $output = @(& $script:Shell @arguments 2>&1 | ForEach-Object { "$_" })
            return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output -join [Environment]::NewLine) }
        } finally {
            $ErrorActionPreference = $previousErrorAction
            foreach ($name in $saved.Keys) {
                [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
            }
        }
    }
}

Describe 'mcp-call.ps1 response handling' {
    It 'writes an exact UTF-8 response body without a BOM or stdout noise' {
        $server = Start-TestJsonRpcServer -ToolsCall $script:Ok
        $out = Join-Path $TestDrive 'reply.json'
        try {
            $run = Invoke-McpCall -Port $server.Port -Extra @('-OutFile', $out)
            $expected = ($script:Ok | ConvertTo-Json -Compress -Depth 32)
            $run.ExitCode | Should -Be 0
            [IO.File]::ReadAllText($out) | Should -Be $expected
            [IO.File]::ReadAllBytes($out)[0..2] | Should -Not -Be @(239, 187, 191)
            (Get-Content -LiteralPath $out -Raw | ConvertFrom-Json).id | Should -Be 2
            $run.Output | Should -Match "MCP addr=127.0.0.1:$($server.Port) tool=viewport_get"
            $run.Output | Should -Not -Match '^127'
        } finally {
            Stop-TestJsonRpcServer $server
        }
    }

    It 'writes JSON-RPC errors and exits nonzero' {
        $server = Start-TestJsonRpcServer -ToolsCall $script:ErrorResponse
        $out = Join-Path $TestDrive 'error.json'
        try {
            $run = Invoke-McpCall -Port $server.Port -Extra @('-OutFile', $out)
            $run.ExitCode | Should -Be 1
            (Get-Content -LiteralPath $out -Raw | ConvertFrom-Json).error.code | Should -Be -32602
            $run.Output | Should -Match 'JSON-RPC error -32602'
        } finally {
            Stop-TestJsonRpcServer $server
        }
    }

    It 'streams response and error headers to stdout when OutFile is absent' {
        $okServer = Start-TestJsonRpcServer -ToolsCall $script:Ok
        try {
            $ok = Invoke-McpCall -Port $okServer.Port
            $ok.ExitCode | Should -Be 0
            $ok.Output | Should -Match 'MCP addr=127\.0\.0\.1:'
            $ok.Output | Should -Match '"jsonrpc":"2.0"'
            $ok.Output | Should -Match '"id":2'
        } finally {
            Stop-TestJsonRpcServer $okServer
        }

        $errorServer = Start-TestJsonRpcServer -ToolsCall $script:ErrorResponse
        try {
            $error = Invoke-McpCall -Port $errorServer.Port
            $error.ExitCode | Should -Be 1
            $error.Output | Should -Match '"code":-32602'
            $error.Output | Should -Match 'JSON-RPC error -32602'
        } finally {
            Stop-TestJsonRpcServer $errorServer
        }
    }

    It 'uses an authenticated descriptor without exposing its token' {
        $token = 'a' * 64
        $server = Start-TestJsonRpcServer -ToolsCall $script:Ok -Token $token
        $descriptor = Join-Path $TestDrive 'mcp.addr'
        Set-Content -LiteralPath $descriptor -Value @("127.0.0.1:$($server.Port)", '1234', $token) -Encoding UTF8
        try {
            $run = Invoke-McpCall -Port 0 -Environment @{ GEN_AUDIO_MCP_ADDR_FILE = $descriptor }
            $run.ExitCode | Should -Be 0
            $run.Output | Should -Not -Match $token
            $run.Output | Should -Match '"id":2'
        } finally {
            Stop-TestJsonRpcServer $server
        }
    }
}
