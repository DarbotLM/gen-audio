# Releases

## v0.2.0

- Viewport: the bitcube video renderer (clip.mp4 is the inverse-HDR cube with
  an audio-synced slice plane, 74a90b4) and the AgentVoice tab with the Yumlog
  Studio voices as flipcard livetiles; "Audio clips" is now "Audio" (4712647).
- MCP serverInfo, ACP agentInfo and the desktop clientInfo announce the crate
  version instead of a literal "0.1.0".
- Release build: `-WavSource <folder>` stages the media.lock.json WAVs from
  another verified copy (only locked sha256 bytes are copied); without them
  the build still refuses, with the staging options in the message.
- Tests: the desktop sidecar boot test can no longer hang the gate (60 s
  deadline, child killed and reaped); the sprawl gate skips the
  toolbox-managed adaptive_framework clones by default and now prunes every
  `-SprawlExclude` path, not only the last.
- Assets: same as v0.1.0 plus the AgentVoice catalog; the Windows assets are
  `Gen-Audio_0.2.0_x64-setup.exe` and `Gen-Audio_0.2.0_x64_en-US.msi`.
## v0.1.0

The initial Gen-Audio release combines the Python SDK, the Windows desktop
app, the Rust MCP/ACP/harness binaries, the versioned voice catalog, and the
asset/cube validation stack.

### Highlights

- Multi-speaker synthesis across Kokoro PyTorch, kokoro-onnx, VibeVoice,
  LiveVoice, Magpie, and Pocket TTS adapters, with explicit refusal paths when
  a runtime or model is unavailable.
- Optional DarbotLM Kokoro 0.9.4, kokoro-onnx 0.6.1, and Misaki
  0.9.4+py315.1 Python 3.15.2 wheels, including multi-language G2P, timed and
  continuous ONNX output, streaming, provider selection, local checkpoints,
  and weighted voice blends.
- A Tauri desktop app with a release-only card viewport, voice/persona
  catalogs, playback, spectrogram and cube views, and a loopback MCP control
  plane. Livetiles use explicit glyph flips, separate UID copy controls, stable
  multi-face indices, and never change faces as a side effect of Attach or
  playback.
- Stateless MCP over HTTP, session-scoped ACP, a JSONL harness, connector
  health surfaces, and honest synthesis/provenance reporting.
- Authenticated desktop-owned MCP startup: random bearer credentials, a
  nonce-checked initialize, loopback-only binding, owner-restricted connection
  descriptors, explicit login-autostart opt-in, and fail-closed frontend
  discovery.
- Confirmed script authoring (`script_analyze`, `draft_plan`, and
  `draft_generate`) with exact prompt hashing, bounded connector output, and
  refusal of mock/truncated/malformed drafts.
- A separate `master_audio` action for issued mono 24 kHz PCM16 WAVs. It uses
  shell-free, deadline-bounded FFmpeg filtering and independently verified
  two-pass loudness normalization, while reporting that it processed audio
  rather than synthesized speech.
- Revisioned cube identity, canonical asset envelopes, generated-vs-manual
  catalog checks, and byte-pinned WAV/cube metadata.
- A Windows node toolbox, reproducible build/test gates, PowerShell 5.1 and
  7 support, locked Tauri tooling, and NSIS/MSI/MSIX/NuGet release packaging
  with the MCP and ACP sidecars.

### Gen-Audio assets

The `v0.1.0` GitHub release contains:

| Asset | Use |
|---|---|
| `gen_audio-0.1.0-py3-none-any.whl` | Installable Python 3.15.2 package. |
| `gen_audio-0.1.0.tar.gz` | Python source distribution. |
| `Gen-Audio_0.1.0_x64-setup.exe` | Windows NSIS installer. |
| `Gen-Audio_0.1.0_x64_en-US.msi` | Windows MSI installer. |
| `Gen-Audio_0.1.0_x64.msix` | Windows MSIX; unsigned unless a signing thumbprint is supplied. |
| `DarbotLabs.GenAudio.0.1.0.nupkg` | Tools-only NuGet package with the Windows app, MCP and ACP binaries. |
| `gen-audio-mcp.exe` | Standalone stateless MCP server. |
| `gen-audio-acp.exe` | Standalone Agent Client Protocol server. |
| `gen-audio-harness.exe` | Standalone trace-writing harness. |
| `SHA256SUMS.txt` | SHA-256 hashes for every release binary/archive. |
| `provenance.json` | Source commit, tag, tool versions, and artifact inventory. |

Run `scripts\build-tauri-windows.ps1 -Mode Full` to build the native Windows
desktop release. The script requires every library WAV to match
`schemas/asset-object/media.lock.json`, builds and handshakes the MCP and ACP
sidecars, verifies their staged hashes, verifies the embedded Vite output, and
proves the packaged executable came from Cargo. NSIS and MSI payloads are
verified before the script creates MSIX and NuGet packages from those same
hashes. Packaging is unsigned by default; signing requires an explicitly
named code-signing certificate already present in a Windows certificate store.

### DarbotLM speech assets

Misaki and Kokoro are pinned at `py315-2026.11.24`; Kokoro-ONNX remains on the
isolated ABI release `py314-2026.11.22` until ONNX Runtime publishes cp315 wheels.

| Repository | Version | Required release assets |
|---|---:|---|
| `DarbotLM/misaki` (`py315`) | `0.9.4+py315.1` | wheel, sdist, `SHA256SUMS.txt` |
| `DarbotLM/kokoro` (`py315`) | `0.9.4` | Python wheel/sdist, `SHA256SUMS.txt` |
| `DarbotLM/kokoro-onnx` (`py314`) | `0.6.1` | isolated wheel/sdist, `provenance.json`, `SHA256SUMS.txt` |

These repositories are private. A user installing the optional Kokoro
extras must authenticate with `gh auth login` before running the supported
toolbox installer. Do not copy a token or a wheel into this repository. Public
`gen-audio` CI cannot anonymously fetch these direct URLs unless the hosting
policy changes or CI receives an explicitly configured private-release
credential.

The November release includes Kokoro's corrected real-valued STFT
reconstruction, the verified ONNX export workflow, and the paired Misaki
wheel. The speech-fork release remains a prerelease and does not claim
cross-platform production certification. The macOS ONNX INT8 regression is
still open, and GPU availability depends on the driver, runtime, model, and
provider. The September `py314-2026.09.17` assets remain immutable historical
snapshots.

### Model and audio policy

Model weights, voice binaries, API keys, and generated podcast audio are not
release assets for this repository. Use local `models/`, voice-pack paths, and
`artifacts/runtime/` as documented in the README. The Git-ignored Library WAVs
are embedded in desktop builds from locked bytes but are not source-controlled.

### Verification

Before publication:

```powershell
pwsh -NoProfile -File scripts\test.ps1 -Tag release -WithWav
pwsh -NoProfile -File scripts\build-tauri-windows.ps1 -Mode Full
uv build
cargo build --locked --release -p gen-audio-mcp -p gen-audio-acp -p gen-audio-harness
```

Record the exact Git commit from `git rev-parse HEAD`, hash every uploaded
asset, and verify that GitHub reports every asset in the `uploaded` state.
The public release notes should point to the same source commit and checksum
manifest.

### Known limits

- The initial desktop binaries are Windows x64. The Tauri configuration names
  Linux and macOS targets, but this release does not claim those installers.
- Model and speech-service setup is external to the package.
- The private DarbotLM release-wheel dependencies require authenticated
  GitHub access or a separately configured CI credential.
- Synthesis refuses missing model files; no fallback tone is represented as
  generated speech.
