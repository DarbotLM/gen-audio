# Wingen Audio

`wingen.audio.exe` is Gen-Audio's self-contained Windows-native execution host.
It is built for .NET 11 and packages the managed runtime, F# contracts,
PowerTorch native dependencies, and the Microsoft eXecution Container (MXC)
bindings into one Windows x64 executable.

## Responsibility split

| Surface | Responsibility |
| --- | --- |
| `wingen.audio.exe` | C# command/HTTP host, bounded JSON, process lifecycle, artifact hashing/publication, MXC execution, and worker protocol handling |
| `WingenAudio.Contracts` | F# worker contracts, placement signals, engine/action parsing, and scoring |
| `WingenAudio.VibeVoice` | Native VibeVoice configuration, tokenizer, WAV input, Qwen2 graph, state-map validation, tokenizer graphs, diffusion graph, and generation capability receipts |
| `PowerTorch` | BF16/CPU tensors, sharded safetensors loading, attention/normalization/embedding/codec primitives, generation primitives, DirectML capability, and PowerShell binary cmdlets |
| `cluster.ps1` / `model_clients.ps1` | Windows inventory, node placement, SSH fan-out, lifecycle, bounded worker HTTP, failover, and output promotion |
| `plugins.ps1` | Native adapter registry plus Windows OpenSSH, WSL SSH, and PowerShell-over-SSH remoting |
| `contracts.ps1` / `execution.ps1` / `preflight.ps1` / `vibevoice.ps1` | PowerShell contracts, device policy, model/voice confinement, capability gates, and native VibeVoice control |

Python is no longer the adapter boundary for VibeVoice or PersonaPlex 7B.
`src/gen_audio/adapters.py` is a narrow compatibility surface over
`legacy_adapters.py`; migrated 7B operations cross `plugins.ps1` and
`wingen.audio.exe`.

## Commands

```powershell
wingen.audio.exe version
wingen.audio.exe powertorch probe
wingen.audio.exe mxc probe
wingen.audio.exe mxc run --command COMMAND --backend processcontainer

wingen.audio.exe worker --action status --engine vibevoice
wingen.audio.exe worker --action generate --engine vibevoice --request request.json --output output.wav
wingen.audio.exe worker-server --engine personaplex --listen http://127.0.0.1:8997 --upstream http://127.0.0.1:18997

wingen.audio.exe vibevoice capability --model-dir PATH
wingen.audio.exe vibevoice synthetic --tokenizer-dir PATH
```

The public VibeVoice/PersonaPlex ports remain `8996` and `8997`. Isolated model
workers use private local ports and are addressed through the native
`worker-server` front end.

## PowerShell plugin and remoting model

`plugins.ps1` selects one of these transports:

1. Windows OpenSSH with the current user's `~\.ssh\config` and identities.
2. A logged-in WSL distribution's own `~/.ssh` when a distribution is present.
3. PowerShell Remoting over SSH using `Invoke-Command -HostName -UserName`.

`Enable-PSRemoting` is available through
`plugins.ps1 -Action EnableRemoting`; it reports the elevated-run command when
the current shell is not administrator. The public status command distinguishes
SSH-based remoting from WinRM endpoint enumeration.

No WSL distribution is currently installed on the primary host. The transport
path is implemented and activates automatically when WSL later has a logged-in
distribution and SSH store.

## Pinned PowerTorch runtime

`runtimes/powertorch.version.json` pins the PowerTorch source commit used by
source builds. The current pin is `8ff5b4f5e8e4bde8ae2853015d2bc0add197f6f1`.

Build-time and runtime probes expose:

- .NET 11 runtime identity
- CPU BF16/F32 support
- DirectML device/capability
- native MXC backend and isolation tier
- exact PowerTorch source pin

The current CPU capability set includes BF16 tensor IO, elementwise operations,
reshape/permute/slice, matmul, softmax, layer/RMS norm, embedding, rotary,
causal GQA attention, Conv1D/ConvTranspose1D, GELU, concat, KV append, argmax,
random normal, sin/cos, sharded safetensors loading, and bounded host residency.

## VibeVoice native state

Verified:

- 10-shard / 1,205-tensor VibeVoice 7B state validation.
- Qwen2 decoder shape construction: 28 layers, 3,584 hidden, 28 attention
  heads, 4 KV heads, 152,064 vocabulary.
- Qwen2 BPE tokenizer, merges, added/special tokens, and 1..4-speaker scripts.
- Mono PCM16 WAV input at 16, 22.05, 24, 44.1, and 48 kHz with 24 kHz output
  resampling.
- Acoustic tokenizer encode/decode, semantic tokenizer encode, and diffusion
  head graph validation.
- KV cache prefill/decode correctness on synthetic dimensions.
- Real checkpoint load into a resident CPU process.

Not yet production-ready:

- Real end-to-end CPU generation performance.
- Production streaming cache equivalence at model dimensions.
- Final generation stop-limit behavior and sustained repeated generation.
- Accelerator transfer and persistent DirectML/CUDA tensor residency.

Do not infer readiness from synthetic capability. `vibevoice capability` and
PowerShell preflight enumerate the missing capabilities and refuse generation
when required work is absent.

## Build and release

Development:

```powershell
pwsh -NoProfile -File G:\PowerTorch\scripts\Build-PowerTorch.ps1 -Configuration Release
dotnet build .\src\wingen.audio\wingen.audio.csproj -c Release
dotnet publish .\src\wingen.audio\wingen.audio.csproj -c Release -r win-x64 --self-contained true
```

Production release:

```powershell
pwsh -NoProfile -File .\scripts\build-tauri-windows.ps1 -Mode Full
```

The release build verifies the MXC and PowerTorch probes, hashes
`wingen.audio.exe`, stages it as a Tauri external binary, and carries that
exact payload through installer/package provenance checks.

## Live rollout and rollback

The native control plane can be rolled out independently of native model
compute. VibeVoice and PersonaPlex remain available through the existing worker
URLs during model-runtime parity work. Model unload/load is a graceful API
operation; a failed native experiment must not terminate production workers or
leave them unloaded.
