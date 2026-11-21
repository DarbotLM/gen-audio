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
            "Multi-turn WAV synthesis through the kokoro-onnx package. "
            "Requires local model and voice files via CLI flags or "
            "GEN_AUDIO_KOKORO_MODEL and GEN_AUDIO_KOKORO_VOICES."
        ),
    ),
    "kokoro_dayour": EngineSpec(
        id="kokoro_dayour",
        kind="synth",
        status="implemented",
        summary=(
            "dayour/kokoro KPipeline on CPU. Misaki is assigned to pipeline.g2p. "
            "Weights are not vendored; fetch hexgrad/Kokoro-82M only when "
            "GEN_AUDIO_ALLOW_HF_DOWNLOAD=1, otherwise use a local .pt pack."
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
            "Misaki grapheme-to-phoneme for Kokoro. custom_entries merge into "
            "lexicon.golds. Kokoro receives those phonemes. Misaki does not "
            "emit a waveform."
        ),
    ),
    "vibevoice": EngineSpec(
        id="vibevoice",
        kind="synth",
        status="implemented",
        summary=(
            "VibeVoice 1.5B adapter. Calls the processor with voice_samples and "
            "generate with cfg_scale, DDPM steps, and the tokenizer. 7B is "
            "GPU-only. Weights are not bundled."
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
