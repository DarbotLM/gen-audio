# AgentYankers

AgentYankers is a DarbotLabs adult comedy podcast. LiveKit agents voiced with PersonaPlex call opted-in marks, improvise from a premise outline, and a show layer turns the highlights into an episode. This repository is the public GenAID Audio SDK. It mixes that episode. The call runner stays in the private package `genaid_audio.agentyankers` (`DarbotLM/darbot-vllm`, schema `agentyankers/v1`).

## What this package does

`gen_audio.agentyankers` is the podcast format:

* Host voiceover and multi-speaker commentary through the VibeVoice adapter (`GenAudioVoiceoverRenderer`). A commentary block is a `Speaker N` script, at most four voices, in first-appearance order.
* Laugh, stinger, and ducked bed cues mixed under the rundown timeline.
* A spectrogram PNG and a cube revision of the finished episode (`spectrogram.py`, `cube_revision.revise`).
* `POST /genaid-audio/agentyankers/episode` to dry-run or render a rundown.

Dry-run writes placeholder tones and sets `speech_synthesized` to false. Render mode calls VibeVoice and raises `EngineRefusal` when the local weights are missing. The refusal writes no episode file.

The compare-list id is `agentyankers` (`kind` `podcast`). It is a format, not a second TTS engine.

## How it connects to the core

```python
from gen_audio.agentyankers import (
    GenAudioVoiceoverRenderer,
    assemble_with_core,
    core_status,
    export_json_schemas,
)
```

`core_status()` reports whether `genaid_audio.agentyankers` imports. When it does, `export_json_schemas()` returns that package's schemas (`load_example_bundle`, `CallRunner`, `EpisodePipeline`, and the rest of the core API stay there). `assemble_with_core(request, bank, renderer)` constructs `EpisodePipeline` with a `GenAudioVoiceoverRenderer`, which is the core's `VoiceoverRenderer`: `render(line, sample_rate)` returns mono float audio at 24 kHz.

When the import fails, this package still dry-runs a rundown. It does not copy the private call runner, the voice library, or model weights.

| Core name | Public SDK |
| --- | --- |
| `EpisodePipeline.assemble` | Called through `assemble_with_core` when the core imports |
| `VoiceoverRenderer` | `GenAudioVoiceoverRenderer` (VibeVoice or dry-run tones) |
| `EnvVibeVoiceRenderer` | Left in the core. This package supplies the renderer that actually calls VibeVoice |
| `select_highlights`, `generate_commentary`, `build_voiceover_plan` | Core show layer. This package renders the rundown those functions already produced |
| `episode_quality_report` / `analyze_audio` | This package writes `quality.json` with the same balanced gate (0.50 s, 1% clipping, a non-empty spectrogram) plus cube before/after |
| `EpisodeExport.write` | `render_episode(..., output_dir=)` writes the same episode file set, and adds `spectrogram.png` and `cube.png` |

## Serve route

`GET /genaid-audio/agentyankers` describes the format. `POST /genaid-audio/agentyankers/episode` accepts JSON:

```json
{
  "mode": "dry_run",
  "seed": 0,
  "logline": "Fictional premise.",
  "output_dir": "episode",
  "marks": [],
  "rundown": {}
}
```

`output_dir` is a relative path under `GEN_AUDIO_ARTIFACT_DIR`. The response lists file names (`episode.wav`, `chapters.json`, `show_notes.md`, `show_notes.json`, `quality.json`, `rundown.json`, `spectrogram.png`, `cube.png`) and the quality report. Machine paths stay out of the JSON.

`recordings` maps a `rec-...` id to a `.wav` file name in the artifact directory (24 kHz). Render mode requires those clips. Dry-run fills a missing clip with a placeholder tone and records that in `asset_warnings`.

An `EpisodeRequest` for `EpisodePipeline.assemble` is an in-process call via `assemble_with_core`. The HTTP route takes a rundown.

## Consent

Every mark in the body passes `require_publish_consent` before audio is written: `opted_in`, scope `publish` or `call_and_publish`, and `disclosure_accepted`. `require_call_consent` is the call-time check (scope `call` or `call_and_publish`) for hosts that share the mark index.

`recording_disclosure()` is the only disclosure sentence. A rundown whose `disclosure_text` differs is refused. Show notes copy that sentence and the consent ids. Contact refs (`agent:`, `lk-identity:`, `room:`) stay out of the notes.

Phone-number-like strings are refused. The error does not include the matched text. Secrets (`api_secret`, tokens, LiveKit keys) are refused; set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `GENAID_PERSONAPLEX_MODEL_BASE`, `GENAID_MODEL_BASE`, and `GENAID_VOICES_DIR` in the environment. The documented voice library root is `/mnt/fschia/genai-media/audio/voice-library`. This repo resolves a relative path under `GENAID_VOICES_DIR` and does not copy the file.

The fictional fixture is `tests/fixtures/agentyankers/dry_run.json`. It has no phone numbers.

## Tests

```bash
pytest tests/test_agentyankers.py
```
