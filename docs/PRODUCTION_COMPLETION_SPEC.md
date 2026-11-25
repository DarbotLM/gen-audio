# Production completion

Python is `requires-python = ">=3.15,<3.16"` with the classifier `Programming Language :: Python :: 3.15` only. Length caps, path confinement, checksum pins, `/health` versus `/ready`, and TTS error classes are Gen-Audio code in `gen_audio.guards` (see `docs/genlm-audio/REVIEW-2026-10-06.md`). This file lists what is still open before a Windows install is the product.

## Blockers

### Windows package and tray service

Acceptance:

- `scripts/build-tauri-windows.ps1` builds `gen-audio-mcp.exe`, runs an MCP `initialize` handshake on `127.0.0.1:8765`, stages `apps/desktop/src-tauri/binaries/gen-audio-mcp-<triple>.exe`, runs `npm ci` and `npm run build` in `apps/desktop`, and fails if `apps/desktop/dist/index.html` is missing. Only then does it invoke `tauri build --bundles nsis,msi`. It does not `cargo build` the desktop crate first. It fails unless an NSIS `*setup.exe` and an `.msi` exist. Launch the installer, not a pre-dist `gen-audio-desktop.exe` (that WebView is `http://localhost:1420`).
- The installer ships `gen-audio.exe` and `gen-audio-mcp.exe` side by side. Launch starts the sidecar, shows the window on the taskbar, and keeps a tray icon. If the sidecar binary is absent, the process falls back to the in-process listener. Closing the window hides it. Quit stops the sidecar.
- The NSIS hook adds a Startup shortcut. A packaged sidecar start also writes `HKCU\...\Run\DarbotGenAudio` unless `GEN_AUDIO_AUTOSTART=0`.
- `mcp_status` reports the bound address, `mode` (`sidecar`, `in-process`, `existing`, or `failed`), and whether `initialize` returned protocol `2025-03-26` and server name `gen-audio`.

`scripts/build-tauri-windows.ps1` is the only build script; SMAX runs it. There is no Linux or macOS build script (the Linux `build-tauri.sh` was retired). CI's rust job checks the desktop crate on Linux with `cargo check -p gen-audio-desktop` and builds no installer.

### GenAID Audio limits on the production path

Acceptance:

- `tests/test_guards.py` (14 tests) passes on Python 3.15.2.
- Speaker scripts parsed by `gen_audio.cast` and the Rust harness reject oversized segments. Environment variables may tighten the caps and cannot raise the hard ceilings.
- Kokoro model resolution honors `GENAID_MODEL_BASE`, `GENAID_LOCAL_FILES_ONLY`, and the SHA256 pin.
- `GET /genaid-audio/health` is liveness. `GET /genaid-audio/ready` is readiness. Gateway health stays HTTP 200 when speech is down; gateway ready is 503. TTS proxy failures are 504, 503, forwarded 4xx, or 502, and the log line has no user-text prefix.
- Generate holds `model_lock`. Podcast disk IO uses `run_off_loop`.

## Gap inventory

Hits of the starter-name, backup, fallback, backlog, intended, stub, mock, placeholder, TODO, FIXME, HACK, not implemented, WIP, temporary, residual, sketch, NYI, dummy, and fake. Cargo.lock and dependency lockfiles are excluded. Historical review prose in `docs/REVIEW_BUGBASH.md` is append-only and is not rewritten.

| Location | Quote | Severity | Close plan |
| --- | --- | --- | --- |
| `docs/SERVE_APIM.md` (previous wording) | called the repo a starter that does not define an HTTP API | High | Closed. The doc now says this package does not start Serve and does not define a synthesis HTTP API. |
| `src/gen_audio/cube_revision.py`, CLI, README, `pyproject.toml` description | "cube-revision sketch" | Low | Keep. The inverse-HDR loop is a heuristic, not a mastering spec. The review says not to pretend it is the missing spectrogram benchmark. |
| `docs/ARCHITECTURE.md` desktop section | "browser DFT / WebGL sketch" | Low | Keep. The window drawing is not matplotlib `specgram`. The Python PNG path is the full spectrogram. |
| `crates/gen-audio-connectors` and connector docs | `mock` | Medium | Keep until a credential and `GEN_AUDIO_CONNECTOR_LIVE=1` are both set. Live calls are implemented behind that gate. |
| `schemas/examples/viewport.example.json` connector cards | `"mode": "mock"` | Low | Example data. The desktop replaces mode from `connector_statuses` when the shell is up. |
| `crates/gen-audio-core/src/bridge.rs` | `fallback` python program name | Low | Keep. It selects `python` or `python3`, then the basename allowlist. It is not a second synthesizer. |
| `docs/SERVE_APIM.md` shared gateway step | "explicit fallback" | Low | Keep. The shared gateway is not the catalog. |
| `src/gen_audio/artifacts.py` | "temporary" job directory | Low | Keep. Scratch disks are why `GEN_AUDIO_ARTIFACT_DIR` exists. |
| `docs/REVIEW_BUGBASH.md` | "Residual risk", "placeholder host", "sketch" | Info | Append-only review log. Do not edit prior rounds. |
| `crates/gen-audio-mcp` serve_health note | "Placeholder host" | Low | Keep. `<node>` is not probed. |
| VibeVoice / PersonaPlex resident workers | Isolated CUDA ABI runtimes | High | Implemented through loopback 7B workers and PowerShell control. Gen-Audio uses Python 3.15.2 while pinned model processes remain on their published CPython 3.12 ABI. |
| Real MP3, LUFS, auth on `/genaid-audio`, GPU cancel, chunk-and-stitch | Review P0/P1 unchecked boxes | High | Open. Listed in the review. Not claimed fixed. |
| Packaged installer executed on this Linux agent | no MSI here | Medium | `scripts/build-tauri-windows.ps1` is the SMAX path. This agent checks the crates and the MCP handshake. |

No `TODO`, `FIXME`, `HACK`, `NYI`, `WIP`, `dummy`, or `fake` markers remain in product source. `backup` and `backlog` have no hits.

## Workstreams

1. **Guards.** Done when `pytest tests/test_guards.py` passes on 3.15.2 and script parsing calls the same ceilings.
2. **Node probes.** Done when `gen_audio.node_http.dispatch` returns 200 for `/genaid-audio/health` and `/genaid-audio/ready` with the model unloaded, and `gateway_ready(None)` is 503.
3. **Desktop tray and MCP.** Done when `cargo check -p gen-audio-desktop` succeeds and the Windows script's handshake grep matches `protocolVersion` `2025-03-26`.
4. **Installers.** Done when SMAX produces NSIS and MSI from `scripts/build-tauri-windows.ps1` and the tray icon is visible after launch.
5. **Compare-list engines.** Open until an adapter exists. Do not mark VibeVoice, PersonaPlex, or Gradio implemented.

## Wingen Audio native worker cutover

The temporary Python inference process is isolated behind the stable `/v1`
worker contract. `wingen.audio.exe` is the Windows production host and is built
self-contained on .NET 11. PowerShell 7 remains the Windows operator and
recovery plane; DarbotLM/MXC owns native containment; F# owns typed domain
contracts and placement; C# owns HTTP, process, JSON, audio-artifact, and MXC
execution. Complete these in order:

1. **Freeze the worker wire contract.** Version the status, preflight, load,
   unload, and generate receipts; define F# error codes, 64 KiB request and
   4 MiB response limits, cancellation, output roots, hashes, and worker health.
2. **Finish MXC residency.** Add persistent `wingen.audio.exe mxc host` lifecycle
   support for ProcessContainer and GPU-capable WSLC: provision, start, execute,
   stop, deprovision, bounded stdout, process tree cleanup, and container-state
   recovery.
3. **Package native model assets.** Produce immutable VibeVoice and PersonaPlex
   tokenizer/model/voice manifests with ONNX or TensorRT graph hashes, providers,
   precision, required GPU/RAM, and migration checks from the current weights.
4. **Implement the F# model domain.** Put engine selection, generation requests,
   placement budgets, tokenizer/voice confinement, cancellation, and stable
   receipts in `WingenAudio.Contracts`.
5. **Implement the C# inference runtime.** Add ONNX Runtime/TensorRT or DirectML
   adapters, tokenizer and PCM pipelines, CUDA/CPU/WSLC placement, streaming,
   bounded buffers, GPU memory accounting, and atomic WAV publication.
6. **Expose the native `/v1` server.** Run the worker endpoints from
   ASP.NET Core inside MXC, preserving the existing loopback-only contract and
   using authenticated coordinator transport for remote nodes.
7. **Own worker lifecycle from Windows.** Switch `start-speech-7b.ps1` and
   `cluster.ps1` to Scheduled Task + MXC host supervision, Job Objects, process
   affinity, port ownership, Event Log, restart budgets, and graceful GPU model
   unload.
8. **Gate on real generation.** Run fixed VibeVoice and PersonaPlex prompts through
   old and new workers; verify WAV rate/channels/duration, deterministic seeds
   where available, memory limits, startup, cancellation, and failure receipts.
9. **Cut over installed products.** Ship `wingen.audio.exe` in NSIS, MSI, MSIX,
   and NuGet; switch `GEN_AUDIO_*_WORKER_URL` defaults to the MXC-backed native
   worker; keep rollback only behind an explicit compatibility flag.
10. **Remove the temporary runtime.** After parity and recovery soak, delete the
    Python `model_workers` package, isolated 7B Python environments, Python
    dependency branches, and Python process launch paths.

### Forward log: replacing seven worker dependencies

On November 24, 2026, [the dependency update](https://github.com/DarbotLM/gen-audio/pull/20)
changed the two `runtimes/requirements-*-hybrid.txt` manifests. It did not port
inference to `wingen.audio`, install a worker, or change the Python 3.15 app
lock. The native host can bound and relay `/v1` traffic; successful relay is
not evidence of native synthesis.

**Release blocker:** the pinned [PersonaPlex provider](https://github.com/NVIDIA/personaplex/blob/3428dfd95309a7f3c84fd93259ded0f810d1ff91/moshi/pyproject.toml)
requires `torch<2.5` and `sentencepiece==0.2`. The pinned
[VibeVoice provider](https://github.com/vibevoice-community/VibeVoice/blob/952326ddb264062466a888cf32a5b2f4e803e16e/pyproject.toml)
requires older Accelerate, Transformers, Datasets, and Gradio; Diffusers
0.38.0 also needs newer Safetensors than the manifest's 0.5.3. Neither
configured CUDA wheel index supplied the requested Torch 2.13.0 Windows
CPython 3.12 build in this review. Do not resolve these conflicts by ignoring
package metadata, downgrading security fixes, or silently running on CPU.

The following work belongs in `src/wingen.audio` (execution) and
`src/WingenAudio.Contracts` (typed requests and receipts). Replace only the
operations the product uses, not entire general-purpose libraries:

| Order | Worker package: old to new | Implementation task | Evidence required before removing it |
|---|---|---|---|
| 1 | `datasets`: `3.5.0` to `5.0.1` | Inventory inference-time reads separately from provider demo/training imports. For required inputs, stream local manifest entries with path confinement, length bounds, and hash checks. If no reads are required, remove the provider import rather than implementing a dataset service. | With downloads disabled and Datasets absent, both workers read the same approved records and reject a missing or mismatched asset. |
| 2 | `sentencepiece`: `0.2.0` to `0.2.1` | Load the pinned PersonaPlex `.model` through a native tokenizer. Preserve normalization, whitespace pieces, unknown IDs, and piece-to-text behavior from `model_workers/personaplex.py`. | Compare IDs and decoded pieces with the reference on multilingual, whitespace, empty, and unknown-token cases. Reject incompatible vocabulary or model hashes before allocation. |
| 3 | `torch`: `2.4.1+cu121` to `2.13.0` | Export the actual VibeVoice and PersonaPlex inference graphs as immutable artifacts. Load a pinned native execution provider, verify tensor shapes and precision at load, and stream bounded PCM on generate. | On an approved GPU node, record provider, device, peak memory, sample rate, channels, output duration, and numerical/audio parity against the reference. Cancel and unload must release the allocation; an unavailable provider must fail explicitly. |
| 4 | `accelerate`: `1.6.0` to `1.15.0` | Extend F# node scoring to a per-model placement plan. Account for host and GPU memory, reserves, and tensor residency; have the C# loader enforce it before loading weights. | Compare plans with the current worker for GPU-only, allowed hybrid, and insufficient-memory cases. No oversubscription or implicit provider change. |
| 5 | `transformers`: `4.51.3` to `5.10.1` | Implement the VibeVoice processor, special-token checks, masks, and generation loop against the approved graphs. Keep ASR and other independent consumers out of the removal decision. | Golden token IDs, mask shapes, cache bounds, stop conditions, and fixed-seed output agree with the reference; incompatible model/tokenizer pairs fail preflight. |
| 6 | `diffusers`: `0.32.2` to `0.38.0` | Read the scheduler parameters from the pinned graph manifest and implement the denoising step schedule used by VibeVoice. Honor the request's inference-step limit and cancellation between steps. | Compare timesteps and intermediate latents at a fixed seed under recorded tolerances, then compare decoded PCM. Unknown schedules fail before generation. |
| 7 | `gradio`: `5.50.0` to `6.16.0` | Use the existing Studio/MCP clients and native `/v1` JSON receipts instead of importing the upstream demo UI. Remove Gradio from the worker dependency graph, not just from the top-level requirements file. | Run status, load, generate, cancel where supported, and unload through the shipped interfaces with no Gradio import, demo listener, or extra port. |

For each row, first record reference behavior and artifact hashes, then land
the native implementation behind the existing contract, then exercise parity
and rollback on the supported hardware. Mark a row complete only when a fresh
release install can run that path with the Python package absent from the
worker graph. Coordinate live model and service changes separately.

### Forward log: scanner reconciliation

The initial GitHub inventory at `1085a73` contained 44 Dependabot records and
29 CodeQL records, not 84 verified findings. The Code Quality endpoint reported
that the feature was not enabled; it is not a source for an inferred extra 11.
Use the scanner and record number together: numbering overlaps.

| Records | Action and closure gate |
|---|---|
| Dependabot 83-125 | GitHub subsequently marked all 43 worker-manifest records fixed after the seven-package update. Verify the separately installed worker inventory and ABI before claiming deployment protection. |
| [Dependabot 1](https://github.com/DarbotLM/gen-audio/security/dependabot/1) | Open: `glib 0.18.5` on the Linux GTK3/WebKit2GTK desktop path; not in the Windows target graph. Move the consuming desktop stack to a supported compatible version with the [iterator fix](https://github.com/advisories/GHSA-wrw7-89jp-8q8g). Require a locked Linux build, tray/WebView smoke check, and no affected version in `cargo tree --locked --target x86_64-unknown-linux-gnu`. Adding parallel `glib 0.20` does not fix the existing path. |
| CodeQL 1-7 | Give CI jobs a read-only token and match literal chunk names safely in the release/fixture test. Verify both build modes and rescan Actions and JavaScript. |
| CodeQL 8-11 | Bound release-gate parsing, validate HTML structurally, and keep request-selected resources under their trusted roots. Exercise valid release and route inputs as well as refusal cases; rescan Python. |
| CodeQL 12-29 | Separate ACP production stderr from test-only assertions. Keep failure locations useful without printing sensitive payloads; preserve each assertion, then rescan Rust. |

Closure means a default-branch scan reports the relevant record fixed. A passing
unit test, a clean Windows dependency tree, and absence of a Code Quality count
are not substitutes for that result.

## D — Desktop honesty (fixtures ≠ podcast)

### Done

- Embedded Tauri release uses `frontendDist` (`../dist`) with `custom-protocol`; launch the installer / `gen-audio.exe` from `tauri build`, not a bare `cargo build` that loads `devUrl` (`http://localhost:1420`).
- Tray: close hides to tray; tray **Quit** stops the sidecar and `app.exit(0)`. `tauri-plugin-single-instance` focuses the existing window on second start (no dual tray). A second start with `--quit` invokes the same `request_quit` path.
- Library slide: livetiles backed by real synth WAVs on disk (`artifacts/library/`, served via `apps/desktop/public/library/`). WAVs are gitignored; JSON/PNG thumbs and `manifest.json` may ship in-tree. Manifest marks `synthesizedSpeech: true` only for real clips; unavailable engines stay `unavailable` with reasons — no fake speech.
- Fixture / example viewport content is labeled as fixture theater in the UI (not podcast speech). Browser DFT / WebGL cube sketch is not the Python matplotlib spectrogram path.
- Guards: `pytest tests/test_guards.py` → 14 passed on Python 3.15.2.

### Open on purpose

- `schemas/examples/viewport.example.json` remains example/fixture data for boot; it is not a generated podcast.
- Connector cards may show `mode: mock` until credentials + `GEN_AUDIO_CONNECTOR_LIVE=1`.
- Compare-list adapters (VibeVoice, PersonaPlex, Gradio, Magpie runtime on SMAX) stay open until a real harness exists — do not stamp them as podcast-ready.
- Cube-revision / browser DFT stay labeled sketch / not-podcast where they are not the full Python PNG spectrogram.

### Not done (product yellow)

- Manual tray-menu Quit click-through on every new build when UI Automation cannot see the Win11 tray (document trigger + PIDs gone). Product also accepts `gen-audio.exe --quit` on a second start, which calls the same `request_quit` path as tray Quit (stop sidecar + `app.exit(0)`).
- Fresh NSIS + MSI from `scripts/build-tauri-windows.ps1` after each dirty UI/tray change when Optimus re-stamps A.
