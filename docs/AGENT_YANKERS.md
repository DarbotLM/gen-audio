# Agent Yankers

Agent Yankers is a DarbotLabs show module in this repo. Calling agents receive a premise outline and one mark from the index, then improvise. A podcast layer plans host lines, crowd-laughter beds, and stingers around those calls. [genaid](https://github.com/darbotlabs/genaid) stitches a finished episode from the manifest this module writes. genaid is not imported here.

The checked-in examples are fictional agents. They are not recordings of people.

## Documents

| Document | Schema | Example |
| --- | --- | --- |
| Premise outline | `schemas/shows/agent_yankers/premise.v1.schema.json` | `examples/agent_yankers/premise.example.json` |
| Marks index | `schemas/shows/agent_yankers/marks.v1.schema.json` | `examples/agent_yankers/marks.example.json` |
| Call session | `schemas/shows/agent_yankers/session.v1.schema.json` | `examples/agent_yankers/session.desk-agent.example.json` |
| Voiceover cues | `schemas/shows/agent_yankers/voiceover.v1.schema.json` | `examples/agent_yankers/voiceover.example.json` |
| Highlight picks | `schemas/shows/agent_yankers/highlights.v1.schema.json` | `examples/agent_yankers/highlights.example.json` |
| Episode manifest | `schemas/shows/agent_yankers/episode.v1.schema.json` | `examples/agent_yankers/episode.example.json` |
| Highlight reel | same episode schema, `kind: highlight_reel` | `examples/agent_yankers/episode.highlights.example.json` |

Python loaders live in `gen_audio.shows.agent_yankers`. `python -m gen_audio.cli.agent_yankers` validates them and prints a planned episode. It does not place a call.

## Consent

Every mark has `is_agent` and a `consent` object (`opt_in`, `recorded`, `recorded_at`, `method`, `record_ref`, `parties`, `statement`). `is_agent: true` means the mark is an agent. `is_agent: false` means a person. Neither value skips the check.

`CallRunner.place` refuses the premise before it asks the transport to probe or open a room when any target is missing recorded consent. Recorded consent means all of the following:

- `opt_in` is true and `recorded` is true
- `recorded_at` is set and `method` is `written` or `recorded_verbal`
- `parties` is both `caller` and `mark`
- `record_ref` is a `consent/*.txt` file under the records directory, the file names that mark, and the file states the opt-in

The example records are written opt-ins for fictional agents (`examples/agent_yankers/consent/`). A mark that fails the check is not written into an episode. `assemble_episode` raises the same refusal, so a manifest cannot smuggle a call segment for that mark.

Recording someone who has not consented is not a path this module implements.

## Session

A session plan is one JSON object per consented mark. PersonaPlex speaks for the caller (`nvidia/personaplex-7b-v1`, stock `voice_code`, `text_prompt` built from the persona plus the outline). LiveKit is the room (`transport: livekit`). `improvise` is true. The credential object names environment variables and does not hold their values:

| Variable | Used for |
| --- | --- |
| `LIVEKIT_URL` | LiveKit server URL |
| `LIVEKIT_API_KEY` | LiveKit API key |
| `LIVEKIT_API_SECRET` | LiveKit API secret |
| `PERSONAPLEX_HF_TOKEN` | Hugging Face token for the gated PersonaPlex weights |
| `GEN_AUDIO_VIBEVOICE_MODEL` | Local VibeVoice-1.5B directory |
| `GEN_AUDIO_AGENT_YANKERS_SFX_DIR` | Directory of laughter and stinger wavs |

Do not commit those values.

## What is stubbed

`StubLiveKitTransport` is the default call transport. It does not import a LiveKit SDK and does not load PersonaPlex. `probe` lists unset variable names and the missing runtimes. `open_session` raises `IntegrationUnavailable` and writes no audio. Setting the four credential variables does not make it runnable.

`StubVibeVoiceShowAudio` is the show-audio implementation.

- Host cues are not rendered. This package does not call VibeVoice for them, and it does not write a substitute tone. `probe.runnable` stays false.
- Laughter and stinger cues copy `<sfx_id>.wav` from `GEN_AUDIO_AGENT_YANKERS_SFX_DIR` when that file is already there (`crowd-laugh-short`, `crowd-laugh-long`, `stinger-open`, `stinger-button`). The copy reports `synthesized_speech: false` and `source: sfx`. A missing file is a refusal, not a generated bed.

Swap either class for a real implementation of `CallTransport` or `ShowAudio` when those runtimes exist. The runners stay the same.

## How genaid consumes the manifest

genaid does not link this package. A genaid script reads the episode JSON (the contract id is `consumer.contract`) and follows `consumer.rules`:

1. Read `examples/agent_yankers/episode.example.json` or the object `assemble_episode` prints. `consumer.name` is `genaid` and `consumer.order` is `segments`.
2. Walk `segments` from first to last. Do not sort or drop by role.
3. Skip a segment whose `status` is `planned` or whose `asset` is null. Do not synthesize audio to fill the gap. The checked-in episode is entirely planned, so a stitch of that file is silence until a later run sets `status` to `rendered` and a relative `asset`.
4. When `status` is `rendered`, resolve `asset` relative to the manifest file and concatenate that wav. Paths stay inside the episode directory (`audio/host-open.wav`). `..` and absolute paths fail the schema.
5. Delivery is mono, 24 kHz (`sample_rate`, `channels`).
6. For `role: highlight`, the segment also carries `in_s` and `out_s` (seconds into the call) and `call_segment_id`. Use that window when cutting the replay. The call segment it points at is in the same file when `kind` is `episode`.
7. A highlight reel (`episode.highlights.example.json`, or `highlight_reel(episode)`) is the same contract with `kind: highlight_reel` and the `call` segments removed. Walk its `segments` the same way. `highlights[]` matches those highlight segments by id.
8. Ignore credentials. The manifest does not contain `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, or `PERSONAPLEX_HF_TOKEN`.

`highlights[]` is the edit list (id, call, mark, window, note) so a script can build a reel without guessing which segments are the jokes.

## Commands

```bash
python -m gen_audio.cli.agent_yankers validate-marks examples/agent_yankers/marks.example.json
python -m gen_audio.cli.agent_yankers validate-episode examples/agent_yankers/episode.example.json \
  --marks examples/agent_yankers/marks.example.json \
  --records examples/agent_yankers
python -m gen_audio.cli.agent_yankers plan \
  --premise examples/agent_yankers/premise.example.json \
  --marks examples/agent_yankers/marks.example.json \
  --voiceover examples/agent_yankers/voiceover.example.json \
  --highlights examples/agent_yankers/highlights.example.json \
  --records examples/agent_yankers \
  --episode-id ay-001 \
  --title "The ticket that was never filed"
python -m gen_audio.cli.agent_yankers probe
```

`plan` prints the planned manifest and exits 1 when a target lacks consent. `probe` exits 0 and reports `runnable: false` until a real transport is wired. After `pip install -e .` the same commands are `gen-audio-agent-yankers`.
