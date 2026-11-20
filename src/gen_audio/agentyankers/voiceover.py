"""VibeVoice host voiceover and multi-speaker commentary.

``GenAudioVoiceoverRenderer`` is the object ``EpisodePipeline`` accepts when
``genaid_audio.agentyankers`` is installed. Dry-run mode writes a deterministic
tone per voice id. Render mode calls the VibeVoice adapter in this repo and
reads reference wavs from ``GENAID_VOICES_DIR``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

from gen_audio.agentyankers.contract import (
    ENV_VOICES_DIR,
    SAMPLE_RATE,
    VibeVoiceRuntimeUnavailable,
)
from gen_audio.audio_io import read_wav
from gen_audio.cast import CastMap, Turn, VoiceAssignment
from gen_audio.guards import resolve_voice_file

_MAX_SPEAKERS = 4


class GenAudioVoiceoverRenderer:
    """Per-line renderer. ``render`` matches the core ``VoiceoverRenderer`` protocol."""

    def __init__(self, mode: str = "dry_run") -> None:
        if mode not in {"dry_run", "render"}:
            raise ValueError("voiceover mode must be dry_run or render")
        self.mode = mode
        self.rendered: list[str] = []
        self.speech_synthesized = False

    def render(self, line: Any, sample_rate: int) -> np.ndarray:
        text, voice_id, relative = _line_fields(line)
        if int(sample_rate) != SAMPLE_RATE:
            raise ValueError("VibeVoice voiceover uses 24000 Hz")
        self.rendered.append(voice_id)
        if self.mode == "dry_run":
            return placeholder_tone(text, voice_id, sample_rate)
        audio = synthesize_commentary(
            [
                {
                    "speaker": voice_id,
                    "text": text,
                    "voice_id": voice_id,
                    "relative_path": relative,
                }
            ],
            sample_rate,
        )
        self.speech_synthesized = True
        return audio


def commentary_script(lines: Sequence[Any]) -> dict[str, Any]:
    """VibeVoice ``Speaker N`` script in first-appearance order, at most four voices."""
    slots: dict[str, dict[str, str]] = {}
    rows: list[str] = []
    for line in lines:
        text, voice_id, relative = _line_fields(line)
        speaker = _speaker_name(line, voice_id)
        if speaker not in slots:
            if len(slots) >= _MAX_SPEAKERS:
                raise VibeVoiceRuntimeUnavailable(
                    "VibeVoice commentary supports at most 4 speakers."
                )
            slots[speaker] = {
                "speaker": speaker,
                "voice_id": voice_id,
                "relative_path": relative or "",
            }
        elif slots[speaker]["voice_id"] != voice_id:
            raise VibeVoiceRuntimeUnavailable(
                f"{speaker} is already assigned a VibeVoice voice in this commentary."
            )
        number = list(slots).index(speaker) + 1
        spoken = " ".join(text.split())
        if not spoken:
            raise ValueError("commentary line is empty")
        rows.append(f"Speaker {number}: {spoken}")
    if not rows:
        raise ValueError("commentary script is empty")
    return {
        "provider": "vibevoice",
        "script": "\n".join(rows),
        "speakers": list(slots.values()),
        "max_speakers": _MAX_SPEAKERS,
    }


def render_multispeaker(lines: Sequence[Any], sample_rate: int, *, mode: str) -> tuple[np.ndarray, dict[str, Any]]:
    """Render a commentary block. Render mode is one VibeVoice call for the block."""
    plan = commentary_script(lines)
    if int(sample_rate) != SAMPLE_RATE:
        raise ValueError("VibeVoice voiceover uses 24000 Hz")
    if mode == "dry_run":
        pieces = [
            placeholder_tone(_line_fields(line)[0], _line_fields(line)[1], sample_rate)
            for line in lines
        ]
        gap = np.zeros(int(0.1 * sample_rate), dtype=np.float32)
        mixed: list[np.ndarray] = []
        for index, piece in enumerate(pieces):
            if index:
                mixed.append(gap)
            mixed.append(piece)
        audio = np.concatenate(mixed) if mixed else np.zeros(1, dtype=np.float32)
        return audio, {**plan, "speech_synthesized": False, "voiceover": "placeholder"}
    if mode != "render":
        raise ValueError("voiceover mode must be dry_run or render")
    audio = _synthesize_turns(_turns_from_script(plan), sample_rate)
    return audio, {**plan, "speech_synthesized": True, "voiceover": "vibevoice"}


def synthesize_commentary(lines: Sequence[Mapping[str, Any]], sample_rate: int) -> np.ndarray:
    """One or more commentary lines through the VibeVoice adapter."""
    plan = commentary_script(
        [
            {
                "text": line["text"],
                "speaker": line.get("speaker") or line["voice_id"],
                "voice": {
                    "voice_id": line["voice_id"],
                    "relative_path": line.get("relative_path"),
                },
            }
            for line in lines
        ]
    )
    return _synthesize_turns(_turns_from_script(plan), sample_rate)


def placeholder_tone(
    text: str,
    voice_id: str,
    sample_rate: int,
    duration: float | None = None,
) -> np.ndarray:
    """Deterministic tone keyed by the voice id. This is not VibeVoice audio."""
    if int(sample_rate) != SAMPLE_RATE:
        raise ValueError("voiceover sample rate must be 24000")
    seconds = duration if duration is not None else min(3.0, max(0.45, 0.045 * max(1, len(text))))
    count = max(1, int(round(float(seconds) * sample_rate)))
    frequency = 140.0 + (sum((index + 1) * ord(char) for index, char in enumerate(voice_id)) % 360)
    time = np.arange(count, dtype=np.float64) / sample_rate
    span = count / sample_rate
    attack = np.minimum(1.0, time / 0.02)
    release = np.minimum(1.0, np.maximum(0.0, (span - time) / 0.04))
    wave = 0.22 * attack * release * np.sin(2 * np.pi * frequency * time)
    return np.ascontiguousarray(wave, dtype=np.float32)


def _turns_from_script(plan: dict[str, Any]) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    by_speaker = {row["speaker"]: row for row in plan["speakers"]}
    for row in plan["script"].splitlines():
        number, _, text = row.partition(": ")
        index = int(number.removeprefix("Speaker ")) - 1
        speaker = plan["speakers"][index]
        if speaker["speaker"] not in by_speaker:
            raise VibeVoiceRuntimeUnavailable("commentary speaker is missing a voice")
        turns.append(
            {
                "speaker": speaker["speaker"],
                "text": text,
                "voice_id": speaker["voice_id"],
                "relative_path": speaker["relative_path"],
            }
        )
    return turns


def _synthesize_turns(turns: list[dict[str, str]], sample_rate: int) -> np.ndarray:
    from gen_audio.adapters import EngineRefusal, probe, synthesize

    found = probe("vibevoice")
    if not found.available:
        raise EngineRefusal("vibevoice", found.missing)
    speakers: dict[str, VoiceAssignment] = {}
    cast_turns: list[Turn] = []
    for index, turn in enumerate(turns, start=1):
        speaker_id = _slot(turn["speaker"], speakers, turn)
        cast_turns.append(
            Turn(
                speaker_id=speaker_id,
                text=turn["text"],
                script_name=turn["speaker"],
                line=index,
            )
        )
    cast = CastMap(
        engine="vibevoice",
        default_lang="en-us",
        default_speed=1.0,
        speakers=speakers,
    )
    with tempfile.TemporaryDirectory(prefix="agentyankers-vo-") as tmp:
        destination = Path(tmp) / "commentary.wav"
        synthesize("vibevoice", cast_turns, cast, destination)
        audio, rate = read_wav(destination)
    if int(rate) != int(sample_rate):
        raise VibeVoiceRuntimeUnavailable(
            f"VibeVoice returned {rate} Hz. AgentYankers episodes use {sample_rate} Hz."
        )
    return np.ascontiguousarray(audio, dtype=np.float32)


def _slot(speaker: str, speakers: dict[str, VoiceAssignment], turn: dict[str, str]) -> str:
    for speaker_id, assignment in speakers.items():
        if assignment.name == speaker:
            return speaker_id
    if len(speakers) >= _MAX_SPEAKERS:
        raise VibeVoiceRuntimeUnavailable("VibeVoice commentary supports at most 4 speakers.")
    speaker_id = str(len(speakers) + 1)
    wav = _resolve_prompt(turn.get("relative_path") or "", turn["voice_id"])
    speakers[speaker_id] = VoiceAssignment(
        speaker_id=speaker_id,
        name=speaker,
        voice=wav,
        lang="en-us",
        speed=1.0,
        framework="vibevoice",
        voice_id=turn["voice_id"],
    )
    return speaker_id


def _resolve_prompt(relative_path: str, voice_id: str) -> str:
    root = os.environ.get(ENV_VOICES_DIR, "").strip()
    if not root:
        raise VibeVoiceRuntimeUnavailable(
            f"{ENV_VOICES_DIR} is unset. Voice files stay in the genaid-voices library."
        )
    relative = relative_path.strip()
    if not relative:
        relative = f"{voice_id}.wav"
    try:
        return resolve_voice_file(relative, root)
    except ValueError as exc:
        raise VibeVoiceRuntimeUnavailable(str(exc)) from exc


def _line_fields(line: Any) -> tuple[str, str, str | None]:
    text = str(_field(line, "text"))
    voice = _field(line, "voice")
    if isinstance(voice, str):
        return text, voice, None
    voice_id = str(_field(voice, "voice_id"))
    relative = _maybe(voice, "relative_path")
    if relative is not None:
        relative = str(relative)
    return text, voice_id, relative


def _speaker_name(line: Any, voice_id: str) -> str:
    speaker = _maybe(line, "speaker")
    if isinstance(speaker, str) and speaker.strip():
        return speaker.strip()
    return voice_id


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _maybe(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)

