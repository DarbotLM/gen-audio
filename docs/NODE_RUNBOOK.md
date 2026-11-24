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

Authenticate the GitHub CLI first (`gh auth login`) with access to the private
DarbotLM package wheels. The speech source step clones missing
`dayour/misaki`, `dayour/kokoro`, and `dayour/kokoro-onnx` repositories under
`adaptive_framework\`, then reuses them on later runs. It compares the current
branch with `origin` before pulling, so an unchanged or locally modified checkout
is preserved. These nested checkouts are ignored by the Gen-Audio repository.
The toolbox never accepts or writes a token.

Use the currently mounted drive when `G:` is unavailable. For this node, pass
`-RepoRoot 'Q:\gen-audio'`.

## What `Node` does

The `Node` path:

1. Clones the selected ref when needed and preserves an existing checkout.
2. Installs Node 26, `uv`, Python 3.15.2, Git, GitHub CLI, FFmpeg, Rust, WebView2, and the
   Visual Studio C++ build tools. Node 26 and Python come from checksum-checked
   archives or `uv`. A missing WinGet package (Git, GitHub CLI, uv, FFmpeg,
   Rustup, Build Tools, WebView2) is installed only when its id is passed to
   `-AcceptPackageAgreement`, for example `-AcceptPackageAgreement GitHub.cli`
   after you have reviewed that package's license. Otherwise the run stops and
   prints the exact `winget install` command so you can install it yourself.
   A missing `uv` is installed from PyPI (`pip install --user uv`) first, and a
   `uv` older than `-UvMinimumVersion` (default 0.9.0) is updated, because older
   releases resolve the final `3.15.2` interpreter. The venv must be a final
   Python `-PythonVersion` (default 3.15.2) release.
   Python wheels come from `GEN_AUDIO_PIP_INDEX_URL` when set, else pip's own
   `global.index-url`, else PyPI; torch wheels come from
   `GEN_AUDIO_TORCH_INDEX_URL` (default `https://download.pytorch.org/whl/cpu`).
   Certificate checks stay on for every host. On a network that refuses TLS to
   `files.pythonhosted.org`, point `GEN_AUDIO_PIP_INDEX_URL` at a mirror that
   serves the locked hashes rather than trusting the host.
3. Exports the frozen all-extras lock, downloads the exact private Misaki,
   Kokoro, and Kokoro-ONNX wheel releases with authenticated `gh`, verifies all
   three SHA-256 values, replaces only the local requirements URLs, installs
   that hash-locked graph, runs `pip check`, and verifies
   `uv lock --check --offline`. Native packages use wheels; the few pure-Python
   Misaki dependencies published only as sdists are allowed and remain
   hash-pinned. It then installs the desktop npm lockfile and builds MCP, ACP,
   and harness.
4. Stages checksum-verified Kokoro ONNX and voice-pack files in `models/`. It
   prefers the normalized a2swe model when that exact registry artifact exists.
   DarbotLM’s package releases contain wheels, not a corrected model graph, so
   this step retains the separately verified real model asset rather than
   claiming that the fork export is included.
5. Writes `artifacts/runtime/node-environment.json`, a checkout-scoped runtime
   descriptor, plus a sourceable compatibility text file. It sets process-only
   environment values and does not mutate the user's persisted environment.
   CPU is the default for both Torch and ONNX; CUDA remains explicit through the
   `GEN_AUDIO_KOKORO_DEVICE` / `ONNX_PROVIDER` overrides.
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
