"""Mix an AgentYankers rundown into an episode.

Segments become the timeline. A bed cue is ducked under the program. The
finished buffer is cube-revised, and a spectrogram PNG is written beside the
export. Consent failures raise before any file is written.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import numpy as np

from gen_audio.agentyankers.beds import apply_gain_db, synthesize_bed, synthesize_laugh, synthesize_stinger
from gen_audio.agentyankers.contract import (
    PRODUCER,
    RECORDING_DISCLOSURE,
    SAMPLE_RATE,
    SCHEMA_VERSION,
    SHOW_NAME,
    AgentYankersError,
    MissingRecording,
    require_publish_consent,
)
from gen_audio.agentyankers.core import core_status
from gen_audio.agentyankers.schema import validate_marks, validate_rundown
from gen_audio.agentyankers.voiceover import GenAudioVoiceoverRenderer, placeholder_tone
from gen_audio.audio_io import as_mono, read_wav, write_wav
from gen_audio.cube_revision import revise
from gen_audio.spectrogram import write_spectrogram

PAUSE_SECONDS = 0.1
_EXPORT_NAMES = {
    "audio": "episode.wav",
    "chapters": "chapters.json",
    "show_notes": "show_notes.md",
    "show_notes_json": "show_notes.json",
    "quality": "quality.json",
    "rundown": "rundown.json",
    "spectrogram": "spectrogram.png",
    "cube": "cube.png",
}


@dataclass
class EpisodeRender:
    """In-memory episode plus the files ``write`` stored, when asked."""

    rundown: dict[str, Any]
    show_notes: dict[str, Any]
    show_notes_markdown: str
    quality: dict[str, Any]
    sample_rate: int
    audio: np.ndarray
    warnings: list[str]
    speech_synthesized: bool
    mode: str
    files: dict[str, str]

    def public_body(self) -> dict[str, Any]:
        """HTTP body. Paths stay as file names so the response has no machine path."""
        return {
            "service": "genaid-audio",
            "format": "agentyankers",
            "schema_version": SCHEMA_VERSION,
            "mode": self.mode,
            "episode_id": self.rundown["episode_id"],
            "speech_synthesized": self.speech_synthesized,
            "core": self.quality["core"],
            "quality": self.quality,
            "files": {key: Path(path).name for key, path in self.files.items()},
            "disclosure": RECORDING_DISCLOSURE,
        }


def render_episode(
    rundown: dict[str, Any],
    marks: list[Any],
    *,
    mode: str = "dry_run",
    output_dir: str | Path | None = None,
    recordings: dict[str, np.ndarray] | None = None,
    seed: int = 0,
    logline: str = "",
) -> EpisodeRender:
    """Render or dry-run one episode from a validated rundown."""
    if mode not in {"dry_run", "render"}:
        raise AgentYankersError("mode must be dry_run or render")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2_147_483_647:
        raise AgentYankersError("seed must be an integer from 0 through 2147483647")
    checked = validate_rundown(rundown)
    roster = validate_marks(marks)
    for mark in roster:
        require_publish_consent(mark)
    if not isinstance(logline, str) or len(logline) > 400:
        raise AgentYankersError("logline must be a string of at most 400 characters")
    warnings: list[str] = []
    renderer = GenAudioVoiceoverRenderer(mode=mode)
    bank = recordings or {}
    pieces: list[tuple[dict[str, Any], np.ndarray]] = []
    for index, segment in enumerate(checked["segments"]):
        audio = _render_segment(
            segment,
            index=index,
            mode=mode,
            renderer=renderer,
            bank=bank,
            seed=seed,
            warnings=warnings,
        )
        pieces.append((segment, as_mono(audio).astype(np.float32)))
    program = _join(pieces, SAMPLE_RATE)
    program, bed_ids = _mix_beds(program, checked["cues"], seed, warnings)
    revised = revise(program, SAMPLE_RATE)
    timed = _time_segments(pieces, SAMPLE_RATE)
    chapters = _chapters(timed, revised.audio.size / SAMPLE_RATE)
    notes = _show_notes(checked, roster, chapters, logline)
    markdown = _markdown(notes)
    quality = _quality(
        revised,
        warnings=warnings,
        mode=mode,
        speech_synthesized=renderer.speech_synthesized,
        written=output_dir is not None,
    )
    quality["beds"] = bed_ids
    result = EpisodeRender(
        rundown={**checked, "segments": timed, "chapters": chapters},
        show_notes=notes,
        show_notes_markdown=markdown,
        quality=quality,
        sample_rate=SAMPLE_RATE,
        audio=revised.audio,
        warnings=list(warnings),
        speech_synthesized=renderer.speech_synthesized,
        mode=mode,
        files={},
    )
    if output_dir is not None:
        result.files = _write(result, Path(output_dir))
    return result


def _render_segment(
    segment: dict[str, Any],
    *,
    index: int,
    mode: str,
    renderer: GenAudioVoiceoverRenderer,
    bank: dict[str, np.ndarray],
    seed: int,
    warnings: list[str],
) -> np.ndarray:
    kind = segment["kind"]
    duration = segment.get("duration_seconds")
    if kind in {"stinger", "outro"}:
        return synthesize_stinger(
            SAMPLE_RATE,
            float(duration or 0.45),
            seed + index,
            outro=kind == "outro" or "outro" in segment["segment_id"],
        )
    if kind == "laugh":
        return synthesize_laugh(SAMPLE_RATE, float(duration or 0.9), seed + index + 1)
    if kind in {"disclosure", "commentary"}:
        line = {
            "text": segment.get("text") or RECORDING_DISCLOSURE,
            "speaker": segment["voice"]["voice_id"],
            "voice": segment["voice"],
        }
        audio = renderer.render(line, SAMPLE_RATE)
        if mode == "dry_run":
            warnings.append(f"voiceover placeholder for {segment['voice']['voice_id']}")
            if duration is not None:
                audio = _fit(audio, float(duration))
        return audio
    if kind == "call_clip":
        ref = segment["recording_ref_id"]
        if ref in bank:
            return _fit(as_mono(bank[ref]).astype(np.float32), float(duration))
        if mode == "dry_run":
            warnings.append(f"call clip placeholder for {ref}")
            return placeholder_tone(segment.get("text") or ref, ref, SAMPLE_RATE, float(duration))
        raise MissingRecording(f"no audio stored for {ref}")
    raise AgentYankersError(f"unknown segment {kind}")


def _join(pieces: list[tuple[dict[str, Any], np.ndarray]], sample_rate: int) -> np.ndarray:
    pause = np.zeros(int(round(PAUSE_SECONDS * sample_rate)), dtype=np.float32)
    chunks: list[np.ndarray] = []
    for index, (_, audio) in enumerate(pieces):
        if index:
            chunks.append(pause)
        chunks.append(np.ascontiguousarray(audio, dtype=np.float32))
    if not chunks:
        raise AgentYankersError("episode timeline is empty")
    return np.concatenate(chunks)


def _mix_beds(
    program: np.ndarray,
    cues: list[dict[str, Any]],
    seed: int,
    warnings: list[str],
) -> tuple[np.ndarray, list[str]]:
    mixed = np.ascontiguousarray(program, dtype=np.float32)
    used: list[str] = []
    for index, cue in enumerate(cues):
        if cue.get("asset_ref"):
            warnings.append(f"procedural cue; asset_ref is not loaded: {cue['cue_id']}")
        if cue["kind"] != "bed" or cue["placement"] != "under":
            continue
        bed = synthesize_bed(SAMPLE_RATE, float(cue.get("duration_seconds") or 2.0), seed + index)
        gain = float(cue["gain_db"]) if "gain_db" in cue and cue["gain_db"] is not None else -36.0
        looped = _loop(bed, mixed.size)
        mixed = mixed + apply_gain_db(looped, gain)
        used.append(cue["cue_id"])
    return np.ascontiguousarray(mixed, dtype=np.float32), used


def _loop(audio: np.ndarray, count: int) -> np.ndarray:
    if audio.size == 0 or count <= 0:
        return np.zeros(max(count, 0), dtype=np.float32)
    repeats = int(np.ceil(count / audio.size))
    return np.tile(audio, repeats)[:count].astype(np.float32)


def _fit(audio: np.ndarray, duration: float) -> np.ndarray:
    target = max(1, int(round(duration * SAMPLE_RATE)))
    values = np.ascontiguousarray(as_mono(audio), dtype=np.float32)
    if values.size == target:
        return values
    if values.size > target:
        return values[:target]
    pad = np.zeros(target - values.size, dtype=np.float32)
    return np.concatenate([values, pad])


def _time_segments(
    pieces: list[tuple[dict[str, Any], np.ndarray]],
    sample_rate: int,
) -> list[dict[str, Any]]:
    pause = PAUSE_SECONDS
    cursor = 0.0
    timed: list[dict[str, Any]] = []
    for index, (segment, audio) in enumerate(pieces):
        if index:
            cursor += pause
        duration = float(audio.size) / sample_rate
        item = dict(segment)
        item["start_seconds"] = cursor
        item["duration_seconds"] = duration
        timed.append(item)
        cursor += duration
    return timed


def _chapters(segments: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    marked = [segment for segment in segments if segment.get("chapter_title")]
    if not marked:
        marked = [segments[0]]
        marked[0] = {**marked[0], "chapter_title": segments[0]["title"]}
    chapters: list[dict[str, Any]] = []
    for index, segment in enumerate(marked):
        start = float(segment["start_seconds"])
        if index + 1 < len(marked):
            end = float(marked[index + 1]["start_seconds"])
        else:
            end = float(duration)
        if end <= start:
            end = start + float(segment["duration_seconds"])
        chapters.append(
            {
                "schema_version": SCHEMA_VERSION,
                "chapter_id": f"ch-{index + 1:02d}",
                "title": str(segment.get("chapter_title") or segment["title"])[:120],
                "start_seconds": start,
                "end_seconds": end,
            }
        )
    return chapters


def _show_notes(
    rundown: dict[str, Any],
    marks: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    logline: str,
) -> dict[str, Any]:
    published = []
    for mark in marks:
        published.append(
            {
                "mark_id": mark["mark_id"],
                "display_name": mark["display_name"],
                "kind": mark["kind"],
                "consent_id": mark["consent"]["consent_id"],
            }
        )
    voices = []
    seen: set[str] = set()
    for segment in rundown["segments"]:
        voice = segment.get("voice")
        if not voice or voice["voice_id"] in seen:
            continue
        seen.add(voice["voice_id"])
        voices.append(voice)
    return {
        "schema_version": SCHEMA_VERSION,
        "show_name": SHOW_NAME,
        "producer": PRODUCER,
        "episode_id": rundown["episode_id"],
        "title": rundown["title"],
        "premise_id": rundown["premise_id"],
        "logline": logline or rundown["title"],
        "disclosure": RECORDING_DISCLOSURE,
        "marks": published,
        "chapters": chapters,
        "voices": voices,
    }


def _markdown(notes: dict[str, Any]) -> str:
    lines = [
        f"# {notes['show_name']}: {notes['title']}",
        "",
        f"Produced by {notes['producer']}.",
        "",
        notes["logline"],
        "",
        "## Recording disclosure",
        "",
        notes["disclosure"],
        "",
        "## Chapters",
        "",
    ]
    for chapter in notes["chapters"]:
        lines.append(f"- {_timecode(chapter['start_seconds'])} {chapter['title']}")
    lines.extend(["", "## Marks", ""])
    for mark in notes["marks"]:
        lines.append(
            f"- {mark['display_name']} ({mark['kind']}) — consent {mark['consent_id']}"
        )
    lines.extend(["", "## Voices", ""])
    for voice in notes["voices"]:
        lines.append(f"- {voice['voice_id']} ({voice['library']}, {voice['provider']})")
    lines.append("")
    return "\n".join(lines)


def _timecode(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _quality(
    revised: Any,
    *,
    warnings: list[str],
    mode: str,
    speech_synthesized: bool,
    written: bool,
) -> dict[str, Any]:
    audio = np.ascontiguousarray(revised.audio, dtype=np.float32)
    frames = _frame_count(audio)
    total = int(audio.size)
    clipping = float(np.mean(np.abs(audio) >= 0.99)) if total else 1.0
    rms = float(np.sqrt(np.mean(audio * audio))) if total else 0.0
    duration = total / SAMPLE_RATE if total else 0.0
    gate_warnings: list[str] = []
    if duration < 0.50:
        gate_warnings.append("shorter than the balanced 0.50s duration target")
    if clipping > 0.01:
        gate_warnings.append("clipping ratio above the balanced 1% target")
    if frames == 0:
        gate_warnings.append("spectrogram has no frames")
    if rms <= 1e-12:
        gate_warnings.append("episode is silent")
    return {
        "schema": "agentyankers/audio-quality/v1",
        "mode": mode,
        "speech_synthesized": speech_synthesized,
        "voiceover": "vibevoice" if speech_synthesized else "placeholder",
        "core": core_status(),
        "thresholds": {
            "profile": "dlm-voicemapper-balanced",
            "minimum_duration_seconds": 0.50,
            "maximum_clipping_ratio": 0.01,
        },
        "clipping_ratio": clipping,
        "duration_seconds": duration,
        "rms": rms,
        "gate": {"passed": not gate_warnings, "warnings": gate_warnings},
        "spectrogram": {
            "frame_count": frames,
            "written": written and frames > 0,
            "file": "spectrogram.png" if written and frames > 0 else None,
        },
        "cube": {
            "before": revised.before.to_dict(),
            "after": revised.after.to_dict(),
            "ops": list(revised.ops),
            "written": written,
            "file": "cube.png" if written else None,
        },
        "asset_warnings": list(warnings),
    }


def _frame_count(audio: np.ndarray) -> int:
    nfft = 1024 if audio.size >= 1024 else 256
    if audio.size < nfft:
        return 0
    hop = nfft // 2
    return 1 + (int(audio.size) - nfft) // hop


def _write(result: EpisodeRender, directory: Path) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {key: directory / name for key, name in _EXPORT_NAMES.items()}
    write_wav(paths["audio"], result.audio, result.sample_rate)
    _dump(paths["chapters"], result.rundown["chapters"])
    paths["show_notes"].write_text(result.show_notes_markdown, encoding="utf-8")
    _dump(paths["show_notes_json"], result.show_notes)
    frames = result.quality["spectrogram"]["frame_count"]
    if frames:
        try:
            write_spectrogram(
                paths["spectrogram"],
                result.audio,
                result.sample_rate,
                title=f"{SHOW_NAME} {result.rundown['episode_id']}",
            )
        except ValueError as exc:
            result.quality["spectrogram"]["written"] = False
            result.quality["spectrogram"]["file"] = None
            result.quality["asset_warnings"].append(str(exc))
            paths.pop("spectrogram")
    else:
        result.quality["spectrogram"]["written"] = False
        result.quality["spectrogram"]["file"] = None
        paths.pop("spectrogram")
    from gen_audio.cube_revision import write_cube_plot

    write_cube_plot(paths["cube"], _metrics(result, "before"), _metrics(result, "after"))
    _dump(paths["quality"], result.quality)
    _dump(paths["rundown"], result.rundown)
    return {key: str(path) for key, path in paths.items()}


def _metrics(result: EpisodeRender, side: str) -> Any:
    from gen_audio.cube_revision import CubeMetrics

    payload = result.quality["cube"][side]
    return CubeMetrics(**payload)


def _dump(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
