"""Engine capabilities for the Python SDK and waveform comparisons.

Status describes an adapter. Use the engine probe to check local runtime and model availability.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineSpec:
    """One compare-list entry."""

    id: str
    kind: str
    status: str
    summary: str


ENGINES: dict[str, EngineSpec] = {
    "kokoro_onnx": EngineSpec(
        id="kokoro_onnx",
        kind="synth",
        status="implemented",
        summary=(
            "DarbotLM kokoro-onnx 0.6.1: timed, streaming, continuous and "
            "provider-selected ONNX synthesis. Requires local model and voice "
            "files; ONNX_PROVIDER selects CPU/CUDA/other installed providers."
        ),
    ),
    "kokoro_dayour": EngineSpec(
        id="kokoro_dayour",
        kind="synth",
        status="implemented",
        summary=(
            "DarbotLM Kokoro 0.9.4 KPipeline with one shared KModel and "
            "CPU/CUDA/MPS selection. Misaki is assigned explicitly; local config, "
            "weights, and .pt packs are preferred, with HF downloads opt-in."
        ),
    ),
    "vibevoice_realtime": EngineSpec(
        id="vibevoice_realtime",
        kind="synth",
        status="implemented",
        summary="VibeVoice Realtime 0.5B through dlm_speech. Preset per turn, with 24 kHz WAV and sample offsets.",
    ),
    "livevoice": EngineSpec(
        id="livevoice",
        kind="synth",
        status="implemented",
        summary="VibeVoice Realtime using the existing livevoice cast framework.",
    ),
    "adaptive": EngineSpec(
        id="adaptive",
        kind="synth",
        status="implemented",
        summary="Uses the synthesis framework specified on each cast speaker and normalizes the output.",
    ),
    "misaki": EngineSpec(
        id="misaki",
        kind="g2p",
        status="library",
        summary=(
            "DarbotLM Misaki 0.9.4+py315.1 grapheme-to-phoneme for English, "
            "Japanese, Chinese, Korean, and Vietnamese plus eSpeak languages. "
            "Transformer G2P stays disabled on Python 3.15.2; no waveform is emitted."
        ),
    ),
    "vibevoice": EngineSpec(
        id="vibevoice",
        kind="synth",
        status="implemented",
        summary=(
            "VibeVoice 1.5B adapter. Calls the processor with voice_samples and "
            "generate with cfg_scale, DDPM steps, and the tokenizer. 7B uses an "
            "isolated CUDA + host-RAM worker. Weights are not bundled."
        ),
    ),
    "personaplex": EngineSpec(
        id="personaplex",
        kind="speech_to_speech",
        status="implemented",
        summary=(
            "PersonaPlex 7B through a resident hybrid Moshi/Mimi worker and DLM "
            "Speech. Requires mono 24 kHz PCM16 input and a raw WAV voice reference."
        ),
    ),
    "magpie": EngineSpec(
        id="magpie",
        kind="synth",
        status="implemented",
        summary=(
            "Magpie TTS through the configured local CUDA runtime and model."
        ),
    ),
    "pocket_tts": EngineSpec(
        id="pocket_tts",
        kind="synth",
        status="implemented",
        summary=(
            "Local Kyutai Pocket TTS. Requires the pocket-tts runtime and voice assets."
        ),
    ),
    "agentyankers": EngineSpec(
        id="agentyankers",
        kind="podcast",
        status="format",
        summary=(
            "AgentYankers episode format. Host voiceover and multi-speaker "
            "commentary use the VibeVoice adapter when local weights are configured. "
            "A dry run writes placeholder tones. Laugh, stinger, and bed cues are "
            "mixed here, then the episode gets a spectrogram and a cube revision. "
            "Live calls stay in genaid_audio.agentyankers when that package is "
            "installed. Voice files are read from GENAID_VOICES_DIR and stay there."
        ),
    ),
}


def list_engines() -> list[EngineSpec]:
    """Return the compare list in a stable id order."""
    return [ENGINES[key] for key in sorted(ENGINES)]


def get_engine(engine_id: str) -> EngineSpec:
    """Return one spec or raise ``KeyError``."""
    try:
        return ENGINES[engine_id]
    except KeyError:
        known = ", ".join(sorted(ENGINES))
        raise KeyError(f"unknown engine {engine_id!r}; known ids: {known}") from None
