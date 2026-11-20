# Gen-Audio Windows node runbook

## One-command bootstrap

Run PowerShell 5.1 or newer. The downloaded toolbox can bootstrap a missing
checkout and will not overwrite an existing dirty tree.

```powershell
$toolbox = Join-Path $env:TEMP 'gen-audio-toolbox.ps1'
Invoke-WebRequest `
  -Uri 'https://raw.githubusercontent.com/darbotlabs/gen-audio/main/scripts/toolbox.ps1' `
  -OutFile $toolbox
pwsh -NoProfile -File $toolbox -Step Node -RepoRoot 'G:\gen-audio'
```

Use the currently mounted drive when `G:` is unavailable. For this node, pass
`-RepoRoot 'Q:\gen-audio'`.

## What `Node` does

The `Node` path:

1. Clones the selected ref when needed and preserves an existing checkout.
2. Installs Node 24, `uv`, Python 3.14, Git, FFmpeg, Rust, WebView2, and the
   Visual Studio C++ build tools.
3. Runs `uv sync --frozen --all-extras`, installs the desktop npm lockfile, and
   builds the MCP, ACP, and harness Rust crates.
4. Stages checksum-verified Kokoro ONNX and voice-pack files in `models/`. It
   prefers the normalized a2swe model when that exact registry artifact exists.
5. Writes `artifacts/runtime/node-environment.json`, a checkout-scoped runtime
   descriptor, plus a sourceable compatibility text file. It sets process-only
   environment values and does not mutate the user's persisted environment.
6. Runs the locked no-WAV validation gate. `-SkipTests` is explicit: it reports a
   degraded result and exits nonzero instead of claiming full completion.
7. Launches and verifies:
   - Node service: the configured `genaid-audio` health route
   - MCP: the configured `gen-audio-mcp` health route
   - UI preview: the configured Darbot Gen-Audio page

Defaults live in `scripts/runtime.json`; the checkout descriptor can override the
ports. The script writes launcher PIDs and service logs under
`artifacts/runtime`, holds a named checkout lease for the whole run, and refuses
to adopt a healthy-looking service without a matching launcher record.

## Desktop package note

The UI build is always installed and validated. The signed/native Tauri package
has an intentional additional gate: the release embeds five library WAV files
that are not committed to Git. When those exact locked files are staged in
`apps\desktop\public\library`, run:

```powershell
pwsh -NoProfile -File scripts\build-tauri-windows.ps1 -Mode Full
```

The node and MCP framework do not depend on those sample clips.

## Resume and verify

Logs live under `%LOCALAPPDATA%\gen-audio\toolbox\logs`. Re-run the same command
to resume; installed dependencies and matching assets are reused. Individual
stages can be selected with `-Step Deps`, `Models`, `Build`, `Test`, `Start`, or
`Verify`.

```powershell
Invoke-RestMethod 'http://127.0.0.1:8002/genaid-audio/health'
Invoke-RestMethod 'http://127.0.0.1:8002/genaid-audio/ready'
Invoke-RestMethod 'http://127.0.0.1:8765/health'
```

