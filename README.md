# gen-audio

Darbot Gen-Audio is a desktop app plus a Python SDK for multi-speaker podcast tooling: a card viewport, fixture spectrogram and inverse-HDR cube views, a stateless MCP server, an Agent Client Protocol agent, a trace-writing harness, and connectors for Copilot, Claude, GPT, and Gemini.

[genaid](https://github.com/darbotlabs/genaid) is a separate JavaScript prompting framework. This repository does not vendor that code.

This repo does not ship model weights, voice binaries, API keys, or podcast renders. The spectrogram panel draws a browser formant map for the agent, voice, duration, perspectives, and loaded engine in the side pane. That map is not speech and not the Python spectrogram. The cube view stays a sine fixture tone.

## Releases

`v0.1.0` is the initial public Gen-Audio release. The GitHub release carries
the Python wheel and source archive, Windows NSIS/MSI installers, an unsigned
by default MSIX, a tools-only NuGet package, native MCP, ACP, and harness
executables, checksums, and source provenance. The release does not bundle
model weights or generated audio.

The optional speech forks are published separately under the pinned
`py315-2026.11.24` tag for Misaki/Kokoro and the isolated `py314-2026.11.22` tag for Kokoro-ONNX in
[`DarbotLM/misaki`](https://github.com/DarbotLM/misaki/releases/tag/py315-2026.11.24),
[`DarbotLM/kokoro`](https://github.com/DarbotLM/kokoro/releases/tag/py315-2026.11.24),
and
[`DarbotLM/kokoro-onnx`](https://github.com/DarbotLM/kokoro-onnx/releases/tag/py314-2026.11.22).
Those repositories are private, so installers use authenticated GitHub CLI
access; the public Gen-Audio package cannot anonymously download their wheels.

See [docs/RELEASES.md](docs/RELEASES.md) for the release matrix, verification
procedure, known limitations, and complete release notes.

## Synthesis engines

The Python 3.15.2 Kokoro stack is pinned to the DarbotLM forks and their
`py315-2026.11.24` release wheels:
[misaki](https://github.com/DarbotLM/misaki) `0.9.4+py315.1`,
[kokoro](https://github.com/DarbotLM/kokoro) `0.9.4`, and
[kokoro-onnx](https://github.com/DarbotLM/kokoro-onnx) `0.6.1`.
They stay optional and import only when an engine is requested.

`kokoro_onnx` accepts a local ONNX graph and voice pack, supports
`create_timed` phoneme timestamps, asynchronous chunk streaming, and
duration-export `continuous=True` synthesis. `ONNX_PROVIDER` selects a
provider such as `CPUExecutionProvider` or `CUDAExecutionProvider`; the fork
refuses an unavailable provider or an implicit CPU fallback. `--provider`,
`--vocab`, `--misaki-g2p`, `--continuous`, `--no-trim`,
`--sentence-pause`, and `--clause-pause` expose the production controls.
`GEN_AUDIO_KOKORO_USE_MISAKI`, `GEN_AUDIO_KOKORO_TRIM`,
`GEN_AUDIO_KOKORO_CONTINUOUS`, and the two `GEN_AUDIO_KOKORO_*_PAUSE`
environment variables carry the same defaults through MCP and the desktop.

`kokoro_dayour` uses one shared DarbotLM `KModel` and one `KPipeline` per
language. It assigns Misaki explicitly, supports local `.pt` voice packs and
weighted blends, and records the same sample-accurate speaker offsets as the
ONNX adapter. `GEN_AUDIO_KOKORO_DEVICE` defaults to `cpu`; `auto`, `cuda`,
`cuda:N`, and `mps` are opt-in. `GEN_AUDIO_KOKORO_TORCH_CONFIG` and
`GEN_AUDIO_KOKORO_TORCH_WEIGHTS` are a paired local config/checkpoint override.
Without those files, `GEN_AUDIO_ALLOW_HF_DOWNLOAD=1` permits the Hugging Face
load; otherwise Hugging Face is forced offline and a missing cache is a refusal.

Misaki is G2P-only. The shipped configurations cover American/British English,
Japanese (`pyopenjtalk`), Chinese (legacy or 1.1), Korean, Vietnamese, and the
Kokoro eSpeak languages Spanish, French, Hindi, Italian, and Brazilian
Portuguese. `gen-audio-voices phonemize CONFIG --text TEXT` prints phonemes but
never creates a waveform.

`vibevoice_realtime` uses the Microsoft Realtime 0.5B provider in `dlm_speech`.
Set `GEN_AUDIO_DLM_SPEECH_URL` to its base URL and, if the worker requires
authentication, set `GEN_AUDIO_DLM_SPEECH_TOKEN` in the renderer environment.
Select a loaded stock preset such as `en-Carter_man`. The script generator
maps the Alice and Frank personas to the stock Emma and Frank presets.
For authored cast maps, `livevoice` also selects the Realtime adapter.
The existing `vibevoice` engine uses the separately configured long-form
checkpoint and its reference WAVs.

```powershell
'VibeVoice is running on this node.' | python -m gen_audio.cli.render_speech --engine vibevoice_realtime --voice en-Carter_man --output speech.wav
```

Install `nemo-speech` from NVIDIA to use Magpie and its NanoCodec decoder on
CPU. Set `GEN_AUDIO_NEMO_SPEECH` to the executable or put it on PATH.
`nemo-speech pull magpie` provisions the model, tokenizer, and codec.
For optional transcript QA, set `GEN_AUDIO_NEMOTRON_ASR_MODEL`
to an installed Nemotron model ID. Pocket TTS uses the separately installed
`pocket-tts` package and its own presets.

For mixed narration use `engine: adaptive` in an authored cast map and name
the synthesis framework and voice on each speaker. Pass the cast with
`gen-audio-generate --engine adaptive --cast-map PATH --prompt-file SCRIPT
--duration-s SECONDS --out-dir DIRECTORY`. Keep the Speaker script and cast
map inside the work directory. The adapter decodes each
runtime's audio, converts it to mono at 24 kHz, and records sample offsets
after conversion. It accepts WAV and declared PCM16 byte orders from the speech
service, including older DLM Speech builds. Each model runs its own neural
codec and keeps its own voice preset format.

## Speech recognition

Use Transformers 5.23.0 with Accelerate for the Microsoft VibeVoice ASR-HF
checkpoint. The command reads local weights and emits JSON with plain text,
speaker segments, and timestamps. Use `--context` for vocabulary hints. Set
`GEN_AUDIO_VIBEVOICE_ASR_MODEL` to the ASR-HF directory to use it for Gen-Audio
transcript QA. `GEN_AUDIO_VIBEVOICE_ASR_CONTEXT` supplies optional QA vocabulary.

```powershell
gen-audio-transcribe-vibevoice recording.wav --model C:\models\vibevoice-asr-hf --context "Darbot, VibeVoice"
```

Use a separate environment for the Microsoft streaming source, Transformers
4.57.6, and either ASR Streaming checkpoint. Install this package in that
environment and expose the upstream source on `PYTHONPATH`. Populate the Qwen
tokenizer cache and run with `HF_HUB_OFFLINE=1`. Streaming emits JSON lines in
checkpoint order and uses the checkpoint's chunk and lookahead settings.

```powershell
gen-audio-transcribe-vibevoice recording.wav --model C:\models\vibevoice-asr-streaming-15 --streaming
```

For an installed Nemotron 3.5 Transcribe 30B-A4B SALM pair, set
`GEN_AUDIO_NEMOTRON_ASR_MODEL` to the decoder and
`GEN_AUDIO_NEMOTRON_ASR_ENCODER` to its matching encoder. The Gen-Audio
transcript QA path sends both to the local NeMo Speech runtime.

## Desktop app

Targets: Windows (WebView2, NSIS, MSI, MSIX and a tools NuGet package), macOS (WebKit, dmg), Linux (webkit2gtk 4.1, deb/appimage). Ship the installer from the build script. Do not launch `target/release/gen-audio-desktop.exe` from a bare `cargo build -p gen-audio-desktop`: that compile leaves the `custom-protocol` feature off, so the WebView loads `devUrl` `http://localhost:1420` and Edge reports `ERR_CONNECTION_REFUSED`. MCP on `127.0.0.1:8765` can still be healthy while that window is dead.

Release builds embed `apps/desktop/dist` (`frontendDist`: `../dist`). `devUrl` is only for `tauri dev`. The scripts build the Vite app and refuse to continue if `apps/desktop/dist/index.html` is missing, then run `tauri build`. The installed app keeps a tray icon and a taskbar button while the window is open. It starts and authenticates its own `gen-audio-mcp` beside the executable on `127.0.0.1:8765` (or a configured `GEN_AUDIO_MCP_ADDR` inside the CSP-approved `8765`-`8770` range), choosing another loopback port in that range when the address is occupied rather than attaching to an unrelated listener. The random bearer credential is sent only in child environment, IPC, HTTP headers, and an owner-restricted `mcp.addr` descriptor; it is never in a URL or process argument. The child must prove the boot nonce through an authenticated `initialize`. The app carries `gen-audio-acp` as another external sidecar. Closing the window hides it. Quit stops the owned MCP sidecar and removes the descriptor. On Windows, login autostart is disabled unless `GEN_AUDIO_AUTOSTART=1` is explicitly set.

```bash
# Windows: scripts\build-tauri-windows.ps1 -Mode Full|NoBundle (see Scripts below), the only build script.
# There is no Linux or macOS build script; CI's rust job runs `cargo check -p gen-audio-desktop` on Linux.

# Windows, from Windows PowerShell 5.1 or PowerShell 7:
# scripts/build-tauri-windows.ps1      # MCP/ACP checks, npm run build, NSIS/MSI, MSIX/NuGet
```

`apps/desktop` declares Node `>=26.0.0 <27`, pins `@tauri-apps/cli` 2.12.6 and
`@tauri-apps/api` 2.12.5, and resolves through the public npm registry named by
`.npmrc`. `scripts/build-tauri-windows.ps1` runs the installed CLI with
`npx --no -- tauri`; it never downloads a Tauri CLI during a build.

```bash
# library crates and ACP / MCP / harness tests
cargo test --workspace --exclude gen-audio-desktop
cargo check -p gen-audio-desktop

# UI
cd apps/desktop
npm ci
npm run build
npm run dev   # browser preview on :1420, or `cargo tauri dev` inside src-tauri
```

The board sits inside a viewport border beside a blue setup pane. Drag a green engine card into the upper half of that pane to load the voice model. **Agent** is a voice persona (Anton, Alice, Khortana, Rocky, and the rest of the catalog, up to 8 on a track). **Voice** is the TTS or G2P model (kokoro-onnx, DarbotLM Kokoro, misaki, and the unavailable slots). Copilot, Claude, GPT, and Gemini stay on the Connectors slide. `af_heart` is a Kokoro pack id referenced by persona Alice, not a Voice option. The `+` next to Agent adds another persona. Duration and a pasted prompt sit under that. Those controls redraw the spectrogram panel for that profile. The map is not speech.

Viewport tabs are Voice models, AgentVoice, Audio, Video and Cube. AgentVoice shows one flipcard per Yumlog Studio speaker voice (imported with `python -m gen_audio.cli.agent_voices import <studio-dir> --set <name>`): the front has the name, appearance count, total time, an appearance timeline, play/scrub and the voice's spectrogram; the back has the voice's inverse-HDR cube and the appearance list. A voice without a WAV says unavailable. The Audio tab (formerly Audio clips; slug `slide:library`, alias `slide:audio`) holds the library clip tiles and the same AgentVoice flipcards. Library tiles with a real `wavUrl` expose play, pause, and a scrubber. If the WAV is not in this build the transport says the file is missing and does not invent audio. Magpie, VibeVoice, and Pocket stay unavailable. Flip a voice-profile tile for tone, purpose, domain, accent, traits, refs, a 2D browser map, and a 3D hook when a library cube JSON exists. Rename on a clip changes the semantic name and the face name in the window only.

Each card is a live tile: the front stays compact, and Enter or Flip turns it. Arrow keys move between cards. Home and End jump. "Clear viewport" shows the empty state inside the same border. Dev and test builds (`VITE_GEN_AUDIO_FIXTURES=1` with a debug Tauri shell) add "Load labeled example" and "Run Python improve on fixture"; release builds have neither control, and the release exe does not register the `viewport_example` or `run_fixture_improve` commands.

The card contract is `schemas/card-viewport.schema.json`. The example board is `schemas/examples/viewport.example.json`.

## MCP, ACP, and harness

```bash
cargo run -p gen-audio-mcp -- --smoke
cargo run -p gen-audio-mcp -- --http 127.0.0.1:8765
cargo run -p gen-audio-acp -- --smoke
cargo run -p gen-audio-harness -- --fixture
```

MCP is stateless: `initialize` stores no session, HTTP does not set `Mcp-Session-Id`, and the listener refuses non-loopback binds. A desktop-owned listener requires a 64-hex bearer token for `/mcp`, control, health, and library reads; the browser uses an `Authorization` header for both fetch and SSE (the token is never placed in the URL). `GET /health` is liveness, and it also counts the HTTP requests this listener refused: `rejected` (per status, e.g. `{"400":0,"401":1,"403":2,"411":0,"415":1}`) and `last_rejected` (`{method, path, status, t}`, with no request body or headers). Every refusal is also one stderr line `gen-audio-mcp http: rejected <method> <path or tool> <code> <reason> peer=<addr> seq=<n>`, where `<n>` numbers this process's refusals from 1, so no two lines are the same text. When the desktop starts the sidecar, it keeps that stderr in `gen-audio-mcp.log` in the Tauri app log dir (on Windows `%LOCALAPPDATA%\labs.darbot.genaudio\logs`), rotating at 1 MiB with three files kept. A line it cannot write (a failed rotation) is counted, never dropped silently: the next line that can be written is preceded by one `gen-audio-desktop: sidecar log dropped <n> line(s): <reason>` marker. Each line gets a unix-ms stamp and is redacted whole before it is capped at 512 bytes: no request bodies, header values, query strings, `token=`/`key=`/`password=` values, emails, home-directory user names or token-shaped strings. `GET /ready` means this MCP listener is up (`speech` is false, models are not loaded). It is not genaid-audio `/ready`. `GET /control` and `GET /control/stream` expose a short loopback command ring (SSE, no session id) for the desktop window. UI tools (`ui_navigate`, `ui_select_tile`, `ui_flip`, `ui_playback`, `ui_set_sidepane`, `ui_generate`, `library_list`, `library_rename`, `library_harvest`, `voice_profile_get`, `voice_profile_list`, `cube_layers`) queue that ring or return catalog data. They do not write speech. `ui_playback` takes `origin` (`user` or `auto`, play only). A `ui_playback` seek waits up to `waitMs` (default 2000, max 10000) for the window, which posts `ui_seek_report` once its seek finishes, and returns `{requested_t, landed_t, ok, reason}`: where the clock actually landed, or `ok: false` with the reason (including "no window reported"). The exact bodies the window posts live in `schemas/examples/control/`; the npm tests assert the desktop builds them and the cargo tests assert the server accepts them. `ui_cube {mode: "single"|"compare", tileId?|uid?}` sets the Cube tab mode (Compare: the clip's Library cube, `library_r3`, beside the same WAV's `pipeline_r2` cube on one playback slice); the Compare button posts the same tool, and `viewport_get` reads `cube_mode` and `cube_compare` back (contract: `schemas/examples/ui_cube.contract.json`). `ui_generate` records a running or unavailable phase and does not call `synth`. `synth` still refuses when the Kokoro model env vars are unset and publishes `phase: refused` with `synthesizedSpeech: false`. The voice-profile contract is `schemas/voice_profile.schema.json`.

ACP is session-scoped because that protocol requires `sessionId`. `session/new` can bind persona agents and a Voice model. A prompt such as `profile alice`, `agent alice rocky voice kokoro_onnx`, or `generate` calls those MCP tools and emits `tool_call` updates. `generate` also calls `synth` when the Voice model has an adapter, and the result stays a refusal until model files are configured. Prompt audio is false. The exact prompts `health` and `status` still return connector health. The harness prints JSONL. It skips synthesis instead of inventing speech. `--fixture` writes a labeled sine WAV. `--improve` runs the Python publish chain on that fixture when the checkout is available.

Connector environment variables, mock behavior, and the live flag are in [docs/CONNECTORS.md](docs/CONNECTORS.md).

## Python SDK

The installable package is `gen_audio`. With local model files it can render a
two-speaker script through DarbotLM Kokoro ONNX or Kokoro PyTorch, then run the
24 kHz publish chain, draw spectrogram PNGs, and produce the cube-revision sketch.

AgentYankers episodes are a format in that SDK. `gen_audio.agentyankers` mixes a rundown with VibeVoice host and multi-speaker commentary, laugh and stinger cues, a ducked bed, a spectrogram, and a cube revision. `POST /genaid-audio/agentyankers/episode` dry-runs or renders that rundown. Live calls stay in `genaid_audio.agentyankers` and are used when that package imports. Voice files are read from `GENAID_VOICES_DIR`. See [docs/AGENTYANKERS.md](docs/AGENTYANKERS.md).

## What actually runs

| Piece | Status |
| --- | --- |
| `Speaker N` parsing and cast-map voice resolution | Implemented |
| Multi-turn synthesis via `kokoro-onnx` 0.6.1 | Implemented: timed, streamed, blended, continuous, CPU/GPU-provider-aware when local model files are provided |
| Publish chain: trim silence, ~70 Hz high-pass, peak ~0.89, resample 24 kHz | Implemented |
| Before/after spectrogram PNGs | Implemented |
| Inverse-HDR / BW95 / clip-fraction revision sketch | Implemented as a heuristic, not a mastering standard |
| Engine compare on WAVs you already have | Implemented as measurement only; it does not rank engines |
| VibeVoice, Magpie, Pocket TTS, DarbotLM Kokoro | VibeVoice and both Kokoro backends have adapters that run when real weights are configured. Magpie and Pocket stay separate optional runtimes |
| AgentYankers episode format | Implemented. Dry-run tones, or VibeVoice commentary when weights are configured. Laugh, stinger, bed, spectrogram, and cube revision |
| Misaki 0.9.4+py315.1 | Implemented as English/Japanese/Chinese/Korean/Vietnamese and eSpeak G2P. It is never presented as a waveform engine |
| Node HTTP process | Not started by import. URL helpers plus `python -m gen_audio.node_http` |
| Tauri desktop card viewport | Implemented. Agent is a persona and Voice is a TTS model. Library tiles play a WAV when the file loads. The spectrogram panel follows that profile. The pipeline cube card stays a fixture; the spatial slide draws library cube JSON when a clip has one |
| Stateless MCP (`gen-audio-mcp`) | Implemented (stdio and loopback HTTP) |
| ACP agent (`gen-audio-acp`) | Implemented handshake and `session/prompt` |
| Harness traces (`gen-audio-harness`) | Implemented. Synth is skipped without weights |
| Copilot, Claude, GPT, Gemini connectors | Implemented. Mock unless a key is set and `GEN_AUDIO_CONNECTOR_LIVE=1` |
| Agent Yankers (`gen_audio.shows.agent_yankers`) | Premise, marks, call-session plan, voiceover plan, and the genaid episode manifest are implemented. The call runner refuses a mark without recorded two-party consent. LiveKit, PersonaPlex, and VibeVoice host lines stay stubbed and write no audio |

kokoro-onnx voice ids in the example cast are `af_heart` (Alice, Speaker 1) and `am_michael` (Frank, Speaker 2). Those ids exist in the Kokoro v1 voice list. A voice id still has to be present in the voice pack you load; this repo does not check that until kokoro-onnx does.

## Agent Yankers

Agent Yankers is a show module: a caller gets a premise outline and a mark, improvises the call, and a podcast layer plans host lines, crowd laughter, and stingers. [genaid](https://github.com/darbotlabs/genaid) stitches the episode from the manifest. The field guide is [docs/AGENT_YANKERS.md](docs/AGENT_YANKERS.md).

Every mark has `is_agent` and a consent record. `CallRunner` refuses a target unless both the caller and the mark have recorded consent, and it does that before a LiveKit session exists. The examples under `examples/agent_yankers/` are fictional agents.

LiveKit, PersonaPlex, and VibeVoice host lines are stubs. They name the missing env vars (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `PERSONAPLEX_HF_TOKEN`, `GEN_AUDIO_VIBEVOICE_MODEL`) and write no audio. A laughter or stinger cue copies a wav you already put in `GEN_AUDIO_AGENT_YANKERS_SFX_DIR`. Do not commit credentials.

```bash
python -m gen_audio.cli.agent_yankers validate-marks examples/agent_yankers/marks.example.json
python -m gen_audio.cli.agent_yankers plan \
  --premise examples/agent_yankers/premise.example.json \
  --marks examples/agent_yankers/marks.example.json \
  --voiceover examples/agent_yankers/voiceover.example.json \
  --highlights examples/agent_yankers/highlights.example.json \
  --records examples/agent_yankers \
  --episode-id ay-001 \
  --title "The ticket that was never filed"
```

## Install

Python 3.15.2 is required (`requires-python = ">=3.15,<3.16"`). Install it from [python.org](https://www.python.org/downloads/) or with uv:

```bash
uv python install 3.15.2
uv venv --python 3.15.2 .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

For a complete Windows node, use the one-command bootstrap from the
[node runbook](docs/NODE_RUNBOOK.md):

```powershell
$toolbox = Join-Path $env:TEMP 'gen-audio-toolbox.ps1'
Invoke-WebRequest `
  -Uri 'https://raw.githubusercontent.com/darbotlabs/gen-audio/main/scripts/toolbox.ps1' `
  -OutFile $toolbox
pwsh -NoProfile -File $toolbox -Step Node -RepoRoot 'G:\gen-audio'
```

It installs all extras, stages the model files, builds the native services,
runs the locked gate, launches Node/MCP/preview, and verifies their health.
The private DarbotLM wheels are downloaded through authenticated GitHub CLI
(`gh auth login`), SHA-256 checked, and substituted into the local requirements
file; no token or wheel is written into this repository.

`soundfile` needs the system `libsndfile` library (`libsndfile1` on Debian/Ubuntu).

The speech extras are granular: `kokoro-onnx`, `kokoro-torch`, `kokoro-gpu`,
`misaki`, and the compatibility aggregate `kokoro`. The toolbox is the
supported installer for the private release wheels. It installs the locked
`kokoro` aggregate and keeps the graph frozen. ONNX synthesis still needs the
model and voice pack on disk:

```powershell
gh auth login
pwsh -NoProfile -File scripts\toolbox.ps1 -Step Build -RepoRoot 'D:\darbot\gen-audio'
$env:GEN_AUDIO_KOKORO_MODEL = 'C:\models\kokoro-v1.0.onnx'
$env:GEN_AUDIO_KOKORO_VOICES = 'C:\models\voices-v1.0.bin'
```

Those filenames are the v1 pair documented by kokoro-onnx. The model assets
remain separate from the DarbotLM package wheels: the code fork has release
wheels but no replacement graph release, so the toolbox verifies the upstream
model asset independently. Do not commit model files here. `.gitignore` ignores
`*.onnx`, `*.bin`, `*.wav`, and `models/`.

The `vibevoice` extra is the locked VibeVoice-1.5B inference runtime (torch
2.14.1+cpu from the PyTorch CPU index, Transformers `>=5.17,<6`, Accelerate
1.15.0, Diffusers 0.39.0, librosa). Native packages resolve to CPython 3.15.2
wheels for Linux x86_64 and Windows AMD64; pure-Python Misaki dependencies may
use hash-locked source archives:

```bash
uv sync --frozen --extra vibevoice
```

It does not include VibeVoice code or weights. Upstream `datasets`,
`gradio`, and `peft` are fine-tuning or demo dependencies and are left out.

`apps/desktop` builds with TypeScript ~7.0.2 (`npm ci`, then `npx tsc --noEmit` and `npm run build`).

## Checkout-local deployment

Keep downloaded `kokoro-v1.0.onnx` and `voices-v1.0.bin` in `models/`.
Keep generated audio, harness results and logs in `artifacts/runtime/`; neither
directory is committed. Install the Python package into `.venv` with the Kokoro
extra before starting the services. The former sibling deployment folders are
not required.

From any working directory, run the checkout's `scripts\run.ps1`:

```powershell
.\scripts\run.ps1 -Service Mcp       # native MCP, loopback :8765
.\scripts\run.ps1 -Service Node      # health/readiness HTTP, loopback :8002
.\scripts\run.ps1 -Service Desktop   # native release app, not Vite
.\scripts\run.ps1 -Service Preview   # optional Vite preview, loopback :1420
.\scripts\run.ps1 -Service Mcp -Check
```

Each invocation runs in the foreground; use a separate terminal or your service
supervisor. `-Port` overrides that service's port. `-Check` prints paths and
arguments without changing files or starting a process. Before an actual MCP,
Node or Preview launch, `run.ps1` reports the PID and process name already
listening on the requested port. The launcher configures
the native app and its child MCP/Python processes with the same checkout,
`.venv`, and `models/` paths, without changing user-wide environment variables.
Model path and checksum guards remain enabled for synthesis, including when
Rust passes Windows extended-length paths. Service startup itself does not
require a loaded model; generation refuses clearly until its real files are
configured. MCP output is confined to `artifacts/runtime/mcp/`;
pre-existing files cannot be overwritten or read as scratch inputs.

Build the native MCP with `cargo build --release -p gen-audio-mcp`. Build the
desktop through `scripts\build-tauri-windows.ps1 -Mode NoBundle` (or `Full` for
installers); its locked library WAV prerequisites still apply.

## Quickstart

From a checkout, with the environment variables above set:

```bash
python scripts/synth_kokoro_onnx.py examples/podcast_script_sample.txt \
  --cast-map voices/cast_map.example.json \
  --provider CPUExecutionProvider \
  --misaki-g2p \
  -o audio/sample.wav
# sample.wav is float32 so peaks outside [-1, 1] are not clipped yet.

python scripts/improve.py audio/sample.wav -o audio/sample-24k.wav

python scripts/voices.py phonemize misaki.ja --text "こんにちは。"

python scripts/spectrogram.py \
  --before audio/sample.wav \
  --after audio/sample-24k.wav \
  --out-dir artifacts/sample

python scripts/cube_revision.py audio/sample-24k.wav \
  -o audio/sample-cube.wav \
  --plot artifacts/sample/cube.png
```

The same commands are installed as `gen-audio-synth`, `gen-audio-improve`, `gen-audio-spectrogram`, and `gen-audio-cube`.

`examples/podcast_script_sample.txt` is a labeled fixture. It is not a news script and not a recording.

Library use of the publish chain, which does not need a model:

```python
from gen_audio.audio_io import read_wav, write_wav
from gen_audio.improve import improve

audio, sample_rate = read_wav("take.wav")
result = improve(audio, sample_rate)
write_wav("take-24k.wav", result.audio, result.sample_rate)
```

Score WAVs rendered elsewhere. This writes measurements; it does not declare a better engine:

```bash
python scripts/compare_wavs.py \
  kokoro_onnx=audio/sample.wav \
  pocket_tts=audio/pocket-render.wav \
  -o artifacts/scores.json
```

`python scripts/compare_wavs.py --list-engines` prints the compare list and the status of each id.

## Script format

```text
# comments are ignored
Speaker 1: Same-line text.
Speaker 2 (Frank):
Following lines stay with this speaker until the next header.
```

The parenthetical name is stored and is not the lookup key. `voices/cast_map.example.json` maps speaker `"1"` and `"2"` to kokoro-onnx voices `af_heart` and `am_michael`, and maps other characters onto VibeVoice, Kokoro torch packs, PersonaPlex, and the streaming presets. Kokoro speeds outside 0.5–2.0 are rejected. The voice schemas, the `GENAID_VOICES_DIR` library, and `gen-audio-voices` are described in [docs/VOICES.md](docs/VOICES.md).

## Publish chain

Order is fixed: trim leading and trailing silence (default −40 dB relative to the file peak, 30 ms pad), zero-phase high-pass at 70 Hz, peak-normalize to 0.89 linear, resample to 24 kHz. Internal pauses are kept. Details and the scratch-directory copy step are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Set `GEN_AUDIO_ARTIFACT_DIR` (or pass `--artifact-dir`) when a job writes under a temporary directory. The helpers copy the finished WAV, PNG, or JSON there.

## Nodes

Per-node base URL: `http://<node>:8002/genaid-audio`

Health: `http://<node>:8002/genaid-audio/health`

`10.1.8.70:8002` is the shared gateway row in the Power Table, not a stand-in for each node. See [docs/SERVE_APIM.md](docs/SERVE_APIM.md).

## Scripts

Windows tooling uses a small set of canonical entry points. Compatibility scripts run under Windows PowerShell 5.1 and PowerShell 7; GPU/native worker and cube entry points use PowerShell 7. Each entry point checks every native exit code and exits non-zero on failure. Extend an existing entry point with a parameter instead of adding an arbitrary script: `scripts/test.ps1` fails on scripts outside its explicit allowlist, including untracked and gitignored files such as anything under `artifacts/`.

| Entry point | What it does |
|---|---|
| `scripts\toolbox.ps1 -Step Node -RepoRoot <path>` | Fresh-node bootstrap: clone or preserve the checkout, install the complete framework and model files, build, test, persist local configuration, launch Node/MCP/preview, and verify health. |
| `scripts\run.ps1 -Service Mcp\|Node\|Desktop\|Preview [-Port n] [-Check]` | Launches against checkout-local `models/`, `.venv/` and `artifacts/runtime/`. Native desktop children inherit the same configuration. `-Check` prints the launch plan without side effects. |
| `scripts\build-tauri-windows.ps1 [-Mode Full\|NoBundle] [-CertificateThumbprint thumb]` | The only build path. Imports the MSVC environment (`Import-VsDevEnv`), checks every locked library WAV, builds and handshakes both `gen-audio-mcp` and `gen-audio-acp`, hash-checks their `externalBin` staging, runs `npm ci`, and invokes the locally installed Tauri CLI. `Full` (default) builds and verifies NSIS/MSI payloads first, then creates MSIX and NuGet packages from those exact proven hashes. `NoBundle` runs `tauri build --no-bundle`, which still embeds `frontendDist`. Packages default to unsigned; an operator may opt into signing with an existing code-signing certificate. The build judges `dist`, `gen-audio.exe`, installer payloads and the staged sidecars by content, never mtime, and prints `BUILD_OK` only after every output and provenance gate passes. Every run, failed ones included, saves its transcript to `target/logs/build-<sha>-<timestamp>.log`. |
| `scripts\cube_layers.ps1 WAV OUT_JSON -Stem name -Engine engine [-Label text] [-Png OUT_PNG]` | PowerShell 7 operator entry point for Library rev-3 cube generation. `.NET 11`/F# owns WAV parsing, STFT, the four layers, preview selection, JSON, and native PNG output inside `wingen.audio.exe`; Python is not launched. |
| `scripts\pack-windows-msix-nuget.ps1` | Allowlisted packaging helper called by `build-tauri-windows.ps1 -Mode Full` after NSIS/MSI verification. It creates `bundle\msix` and `bundle\nuget` from verified `gen-audio.exe`, MCP and ACP release binaries, rechecks archive payload hashes, and signs both only when `-CertificateThumbprint` names an existing certificate with a private key. |
| `wingen.audio.exe` | Self-contained .NET 11 Windows-native execution host. C# owns worker HTTP, JSON limits, atomic artifact promotion, PowerTorch runtime access, and MXC execution; F# owns worker contracts and placement scoring. `worker` drives the `/v1` model-worker API, `mxc` uses DarbotLM/MXC ProcessContainer or WSLC, and `powertorch probe` reports the pinned native tensor/runtime capabilities. |
| `scripts\cluster.ps1` / `scripts\model_clients.ps1` | PowerShell 7 production control plane. Cluster control adds CIM, GPU, volume, worker, path, and native-host inventory with SSH and fallback. Model clients prefer `wingen.audio.exe` when available and retain PowerShell HTTP as a resilient extension path. |
| `scripts\plugins.ps1` | Native adapter registry and remoting boundary. VibeVoice 7B and PersonaPlex dispatch through `wingen.audio.exe`, with Windows `~\.ssh`, logged-in WSL SSH state, or PowerShell Remoting. `adapters.py` remains only as a compatibility bridge for not-yet-migrated engines. |
| `scripts\contracts.ps1`, `scripts\execution.ps1`, `scripts\preflight.ps1`, `scripts\vibevoice.ps1` | PowerShell-native VibeVoice contract, placement, safetensors/tokenizer/WAV preflight, and generation orchestration. The entry point imports the pinned DarbotLM/PowerTorch binary module and fails closed with named capability gaps until every required 7B operator is implemented; it never launches Python. |
| `scripts\test.ps1 [-Tag name] [-Skip Pssa,Sprawl,Pester,Regen,Cargo,Npm] [-SprawlExclude path] [-FromLock \| -WithWav]` | Runs an EOL preflight, PSScriptAnalyzer 1.24.0, the script-sprawl gate, native Pester 5.7 contracts under `runtimes/powershell`, generated-output verification, `cargo test --workspace`, the desktop TypeScript gates, and a native PowerShell dist release gate. Pester tests use PowerShell's `TestDrive` and clean up automatically. `-FromLock` uses locked WAV facts in CI; `-WithWav` proves staged WAV bytes. Logs go to `artifacts/test-logs/`. |
| `scripts\mcp-call.ps1 -Tool <name> [-ArgsJson <json>] [-Port <n>]` | One MCP `tools/call` over HTTP. Finds the server from `-Port`, `GEN_AUDIO_MCP_ADDR`, `127.0.0.1:8765`, then the loopback ports a `gen-audio` process listens on, and checks `initialize` says `gen-audio`. Example: `scripts\mcp-call.ps1 -Tool ui_navigate -ArgsJson '{"slide":"slide:library"}'`. |
| `scripts\ui-shot.ps1 -Out <png>` | Captures the Gen-Audio window with Win32 `PrintWindow`. No input injection and no screen-scrape fallback. Fails on a blank capture and prints the PNG's sha256. |

`scripts/lib/devenv.ps1` holds the shared helpers (`Import-VsDevEnv`, `Invoke-Checked`) and is the only file that may source `vcvars64.bat`. The remaining `scripts/*.py` files are compatibility shims into `gen_audio.cli`. Library rev-3 cube JSON/PNG comes from `scripts\cube_layers.ps1 WAV OUT_JSON -Stem STEM -Engine ENGINE [-Label LABEL] [-Png PNG]`; the F# generator records its own normalized source hash. The existing Python manifest command still synchronizes legacy cube blocks while the rest of manifest ownership moves native. The Library manifest has one home, `apps/desktop/public/library/manifest.json`. Vite copies it into `dist` at build time.

## Layout

```text
src/gen_audio/          installable package
  cast.py               Speaker N parsing and cast maps
  voices/               voice descriptors, framework registry, catalog
  synth_kokoro_onnx.py  multi-turn kokoro-onnx render
  improve.py            24 kHz publish chain
  spectrogram.py        before/after PNG helpers
  guards.py             length, path, checksum, and TTS error guards
  gateway.py            /health versus /ready and proxy status classes
  node_http.py          loopback genaid-audio health, ready, and AgentYankers routes
  agentyankers/         AgentYankers rundown mix, consent, and VibeVoice commentary
  cube_revision.py      serial inv-HDR / BW95 / clip_frac sketch
  cube_layers.py        compatibility reference for the native F# Library rev-3 generator
  compare.py            measure existing WAVs
  engines.py            compare-list registry
  serve.py              per-node URL and health-body helpers
  shows/agent_yankers/  premise, consented marks, call plan, genaid episode manifest
scripts/                CLI entry points for the modules above
schemas/shows/agent_yankers/   JSON Schema 2020-12 for the show documents
examples/agent_yankers/        fictional agent-only premise, marks, and episode
docs/AGENT_YANKERS.md
voices/                 example voices, G2P configs, catalog, cast map
examples/podcast_script_sample.txt
docs/ARCHITECTURE.md
docs/VOICES.md
docs/SERVE_APIM.md
```

## Tests

```powershell
pwsh -NoProfile -File .\scripts\test.ps1
```

PowerShell, build-script, runner, and native runtime orchestration contracts run
in the Pester modules under `runtimes/powershell`. Rust and TypeScript own
product contract coverage; the Python runtime remains only behind production
audio/model entry points. A real kokoro render is a manual step once the ONNX
files are on disk.

## License

MIT. See [LICENSE](LICENSE).


> Runtime cutover note: `plugins.ps1` and `wingen.audio.exe` own the Windows 7B control plane. VibeVoice and PersonaPlex can use explicit CPU, DirectML, CUDA, or hybrid placement; their upstream compute backend remains isolated only until the pinned PowerTorch graph reaches verified parity. Kokoro-ONNX remains separately isolated until its native runtime migration completes.
