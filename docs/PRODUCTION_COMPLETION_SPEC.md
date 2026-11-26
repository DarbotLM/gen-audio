# Production acceptance contract

This document defines release requirements, not a running project status report.
Implementation and operator instructions belong in [ARCHITECTURE.md](ARCHITECTURE.md),
[WINGEN_AUDIO.md](WINGEN_AUDIO.md), [NODE_RUNBOOK.md](NODE_RUNBOOK.md) and
[RELEASES.md](RELEASES.md). Build and runtime evidence must come from actual
commands, artifacts, CI and security findings; a document is not proof.

## Windows release

- Use `scripts/build-tauri-windows.ps1 -Mode Full` for the native release;
  the existing build owns staging, MCP/ACP handshakes, native host publication,
  frontend compilation, payload hashes and NSIS/MSI/MSIX/NuGet packaging.
- A packaged launch must display the window and tray, start the owned MCP
  listener, hide on close and terminate the sidecar on Quit. Exercise actual
  tray behavior or the equivalent packaged `--quit` path and verify PIDs
  and ports are released.
- The embedded frontend must use the shipped distribution, never a development
  Vite server. Library media must match the locked byte identities.
- Run the canonical validation path in [RELEASES.md](RELEASES.md); do not
  replace failed native checks with mock output or additional launcher scripts.

## Runtime, security and media acceptance

- Enforce bounded text, script, audio, response and model-resource contracts;
  confine paths and validate pinned model hashes. Environment overrides may
  tighten limits, never raise hard ceilings.
- Keep liveness distinct from model readiness. Unavailable speech backends
  must not produce synthetic success receipts. Keep request text and credentials
  out of logs.
- Authorize loopback MCP and control endpoints; verify origin checks, path
  confinement, and cancellation behavior. Authenticate any non-loopback
  worker transport before treating it as production-ready.
- Preserve real audio and generated-media identities. Fixture signals and
  example documents must remain explicitly labeled and outside release speech
  claims. Never substitute a different clip's cube or an invented waveform.
- Verify the actual output bitstream before advertising codec support. Verify
  loudness and true peak for the mastering path; distinguish a timed-out
  request from a confirmed stopped worker.
- Verify the locked dependency graph on the shipping targets. Linux desktop
  release acceptance includes resolving the GTK/glib iterator advisory
  (GHSA-wrw7-89jp-8q8g) in the actual consuming graph, not adding an unrelated
  parallel package version. Security closure is established in the GitHub
  security scanner, not by an assertion in this file.

## Native worker cutover

The stable worker contract remains the boundary. PowerShell owns Windows
operations, F# owns typed contracts and placement, and the native C# host and
pinned PowerTorch/MXC runtime own execution. Keep the currently working
provider path until end-to-end parity and recovery gates pass.

Before retiring a provider dependency, inventory its actual inference-time
use and demonstrate its replacement on supported hardware. In particular,
verify tokenizer IDs, checkpoint hashes, cache equivalence, finite PCM output,
generation performance, accelerator residency, cancellation, repeat generation,
unload and rollback. Synthetic graph checks and successful HTTP relay do not
establish model-generation parity.

| Order | Worker package: old to new | Implementation task | Evidence required before removing it |
|---|---|---|---|
| 1 | `datasets`: `3.5.0` to `5.0.1` | Inventory inference-time reads separately from provider demo/training imports. For required inputs, stream local manifest entries with path confinement, length bounds, and hash checks. If no reads are required, remove the provider import rather than implementing a dataset service. | With downloads disabled and Datasets absent, both workers read the same approved records and reject a missing or mismatched asset. |
| 2 | `sentencepiece`: `0.2.0` to `0.2.1` | Load the pinned PersonaPlex `.model` through a native tokenizer. Preserve normalization, whitespace pieces, unknown IDs, and piece-to-text behavior from `model_workers/personaplex.py`. | Compare IDs and decoded pieces with the reference on multilingual, whitespace, empty, and unknown-token cases. Reject incompatible vocabulary or model hashes before allocation. |
| 3 | `torch`: `2.4.1+cu121` to `2.13.0` | Export the actual VibeVoice and PersonaPlex inference graphs as immutable artifacts. Load a pinned native execution provider, verify tensor shapes and precision at load, and stream bounded PCM on generate. | On an approved GPU node, record provider, device, peak memory, sample rate, channels, output duration, and numerical/audio parity against the reference. Cancel and unload must release the allocation; an unavailable provider must fail explicitly. |
| 4 | `accelerate`: `1.6.0` to `1.15.0` | Extend F# node scoring to a per-model placement plan. Account for host and GPU memory, reserves, and tensor residency; have the C# loader enforce it before loading weights. | Compare plans with the current worker for GPU-only, allowed hybrid, and insufficient-memory cases. No oversubscription or implicit provider change. |
| 5 | `transformers`: `4.51.3` to `5.10.1` | Implement the VibeVoice processor, special-token checks, masks, and generation loop against the approved graphs. Keep ASR and other independent consumers out of the removal decision. | Golden token IDs, mask shapes, cache bounds, stop conditions, and fixed-seed output agree with the reference; incompatible model/tokenizer pairs fail preflight. |
| 6 | `diffusers`: `0.32.2` to `0.38.0` | Read the scheduler parameters from the pinned graph manifest and implement the denoising step schedule used by VibeVoice. Honor the request's inference-step limit and cancellation between steps. | Compare timesteps and intermediate latents at a fixed seed under recorded tolerances, then compare decoded PCM. Unknown schedules fail before generation. |
| 7 | `gradio`: `5.50.0` to `6.16.0` | Use the existing Studio/MCP clients and native `/v1` JSON receipts instead of importing the upstream demo UI. Remove Gradio from the worker dependency graph, not just from the top-level requirements file. | Run status, load, generate, cancel where supported, and unload through the shipped interfaces with no Gradio import, demo listener, or extra port. |

Pinned upstream provider constraints must be checked against the immutable
worker manifests before changing package versions. Never resolve incompatible
metadata by ignoring requirements, silently changing the provider, or claiming
that a newer manifest proves a working installed runtime. The native worker
must satisfy the same public contract and produce real, hashed audio artifacts
before removing the compatibility compute path.
