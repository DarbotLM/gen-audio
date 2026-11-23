"""Import Yumlog Studio AgentVoice sets into the asset object model.

Yumlog Studio's "Detect agent voices" writes one ``agent-voices.json``
(``urn:yumlog:agent-voices:v1``) per recording plus one mono PCM WAV of
combined source excerpts per detected speaker (``StudioAgentVoices.fs``,
``DesktopLibrary.fs``). This module is the import path into Gen-Audio:

* every source voice becomes one row of ``voices`` in
  ``apps/desktop/public/library/agentvoice/assets.json`` (so the row count is
  the source count, honest rows included);
* a voice whose WAV is present and matches the sha256 Yumlog recorded becomes
  a v1 ``audio_clip`` (real recorded speech: ``synthesized_speech`` false,
  claim ``real_wav``), a ``spectrogram_2d`` made by
  :mod:`gen_audio.spectrogram_strip` (the same strip the Audio tiles use) and
  a rev 3 ``cube_ihdr`` (plus its four ``layer`` envelopes) made by
  :func:`gen_audio.cube_layers.library_cube` (the Library cube method);
* a voice without a usable WAV stays a row with ``status: unavailable`` and a
  reason. Nothing stands in for missing audio.

Uids are minted with :func:`gen_audio.identity.mint`, the same preimage as
the Rust minter; ``crates/gen-audio-core/tests/agent_voice_catalog.rs`` runs
``validate_set`` over this catalog together with the Library catalog.

The appearance timeline (source-recording seconds) is unhashed data under
``extensions["x-yumlog-agent-voice"]``. ``body.segments`` holds the same
appearances in the combined-excerpt WAV's own time, so a tile can seek to one.

The WAVs are gitignored (``*.wav``). Only the catalog, the strip PNGs and the
cube JSON/PNG files are committed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from gen_audio.audio_io import read_wav
from gen_audio.cube_layers import LAYER_COLORS, LAYER_NAMES, library_cube
from gen_audio.identity import mint, ms_from_frames, round_half_up
from gen_audio.spectrogram_strip import StripParams, colormap, strip, write_png

YUMLOG_SCHEMA = "urn:yumlog:agent-voices:v1"
SCHEMA_VERSION = "1.0.0"
UID_SCHEME = "ga1"
CATALOG_DIR = "agentvoice"
CATALOG_NAME = "assets.json"
ENGINE = "yumlog_agent_voice"
EXTENSION = "x-yumlog-agent-voice"
GENERATOR = "src/gen_audio/agent_voices.py"
# The widest strip a tile draws; long voices get fewer columns per second.
MAX_STRIP_COLUMNS = 8000
CUBE_PNG_SIZE = (480, 360)

_SET = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_ALIAS = re.compile(r"^[a-z0-9_]{1,64}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"

REPO_ROOT = Path(__file__).resolve().parents[2]
LIBRARY = REPO_ROOT / "apps" / "desktop" / "public" / "library"


class AgentVoiceSourceError(ValueError):
    """The Yumlog manifest is not a usable ``urn:yumlog:agent-voices:v1`` document."""


@dataclass(frozen=True)
class SourceVoice:
    speaker_id: str
    alias: str
    wav_name: str
    sha256: str
    duration_s: float
    turns: tuple[tuple[float, float], ...]
    artifact_id: str | None


@dataclass(frozen=True)
class SourceSet:
    schema: str
    method: str
    recording_sha256: str
    recording_duration_s: float
    voices: tuple[SourceVoice, ...]


def _number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise AgentVoiceSourceError(f"{where} must be a finite number >= 0")
    return float(value)


def load_source(path: Path | str) -> SourceSet:
    """Parse and check a Yumlog ``agent-voices.json``. Raises :class:`AgentVoiceSourceError`."""
    manifest = Path(path)
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise AgentVoiceSourceError(f"{manifest}: {error}") from error
    if not isinstance(doc, dict) or doc.get("schema") != YUMLOG_SCHEMA:
        raise AgentVoiceSourceError(f"{manifest}: schema is not {YUMLOG_SCHEMA}")
    voices = doc.get("voices")
    if not isinstance(voices, list):
        raise AgentVoiceSourceError(f"{manifest}: voices is not a list")
    seen: set[str] = set()
    parsed: list[SourceVoice] = []
    for index, voice in enumerate(voices):
        where = f"voices[{index}]"
        if not isinstance(voice, dict):
            raise AgentVoiceSourceError(f"{where} is not an object")
        alias = voice.get("alias")
        if not isinstance(alias, str) or not _ALIAS.match(alias):
            raise AgentVoiceSourceError(f"{where}.alias {alias!r} is not [a-z0-9_]{{1,64}}")
        if alias in seen:
            raise AgentVoiceSourceError(f"{where}.alias {alias} appears twice")
        seen.add(alias)
        name = voice.get("name")
        if not isinstance(name, str) or Path(name).name != name or not name.lower().endswith(".wav"):
            raise AgentVoiceSourceError(f"{where}.name {name!r} must be a bare .wav file name")
        sha = voice.get("sha256")
        if not isinstance(sha, str) or not _SHA.match(sha.lower()):
            raise AgentVoiceSourceError(f"{where}.sha256 is not a sha256")
        turns = []
        for turn_index, turn in enumerate(voice.get("turns") or []):
            start = _number(turn.get("startSeconds") if isinstance(turn, dict) else None, f"{where}.turns[{turn_index}].startSeconds")
            end = _number(turn.get("endSeconds") if isinstance(turn, dict) else None, f"{where}.turns[{turn_index}].endSeconds")
            if end <= start:
                raise AgentVoiceSourceError(f"{where}.turns[{turn_index}] ends before it starts")
            turns.append((start, end))
        if not turns:
            raise AgentVoiceSourceError(f"{where} has no turns")
        speaker = voice.get("id")
        artifact = voice.get("artifactId")
        parsed.append(
            SourceVoice(
                speaker_id=speaker if isinstance(speaker, str) else f"speaker-{index + 1:02d}",
                alias=alias,
                wav_name=name,
                sha256=sha.lower(),
                duration_s=_number(voice.get("durationSeconds"), f"{where}.durationSeconds"),
                turns=tuple(turns),
                artifact_id=artifact if isinstance(artifact, str) else None,
            )
        )
    recording_sha = doc.get("sourceSha256")
    return SourceSet(
        schema=YUMLOG_SCHEMA,
        method=str(doc.get("method") or "unknown"),
        recording_sha256=recording_sha.lower() if isinstance(recording_sha, str) and _SHA.match(recording_sha.lower()) else "",
        recording_duration_s=_number(doc.get("durationSeconds", 0), "durationSeconds"),
        voices=tuple(parsed),
    )


# --------------------------------------------------------------- envelopes


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def glyph_from_uid(uid: str) -> str:
    """Two braille cells from digest bytes 0-1 (Rust ``asset::glyph_from_uid``)."""
    body = uid.rsplit(":", 1)[1]
    acc = 0
    for char in body[:4]:
        acc = (acc << 5) | _ALPHABET.index(char)
    top = acc >> 4
    return chr(0x2800 + (top >> 8)) + chr(0x2800 + (top & 0xFF))


def _nfc(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {key: _nfc(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_nfc(item) for item in value]
    return value


def envelope(
    *,
    kind: str,
    legacy_id: str,
    title: str,
    summary: str | None,
    status: str,
    fields: dict,
    media: list[dict],
    src: list[str],
    honesty: dict,
    provenance: dict,
    body: dict,
    relations: dict | None = None,
    extensions: dict | None = None,
) -> dict:
    """One v1 envelope, key order and minting as Rust ``asset::build_envelope``."""
    fields = _nfc(fields)
    media = sorted(media, key=lambda item: (item["role"], item["sha256"]))
    src = sorted(set(src))
    uid = mint(kind, fields, [{"role": item["role"], "sha256": item["sha256"]} for item in media], src)
    display = {"title": title}
    if summary:
        display["summary"] = summary
    display["glyph"] = glyph_from_uid(uid)
    out = {
        "schema_version": SCHEMA_VERSION,
        "uid_scheme": UID_SCHEME,
        "kind": kind,
        "uid": uid,
        "legacy_id": legacy_id,
        "status": status,
        "fields": fields,
        "media": media,
        "src": src,
        "relations": relations or {},
        "honesty": honesty,
        "provenance": provenance,
        "display": display,
        "body": body,
    }
    if extensions:
        out["extensions"] = extensions
    return out


def _media(library: Path, rel: str, role: str, mime: str) -> dict:
    path = library / rel
    return {"role": role, "path": rel, "sha256": _sha256(path), "bytes": path.stat().st_size, "mime": mime}


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def strip_params(duration_s: float) -> StripParams:
    """Library strip parameters, with fewer columns per second for long voices (at most MAX_STRIP_COLUMNS wide)."""
    base = StripParams()
    per_second = max(1, min(base.columns_per_second, int(MAX_STRIP_COLUMNS // max(duration_s, 1e-9))))
    return StripParams(base.n_fft, per_second, base.bands, base.f_min, base.f_max, base.db_floor)


def write_spectrogram(samples: np.ndarray, rate: int, png: Path, params: StripParams) -> dict | None:
    """The Audio-tile strip for one voice. None when the WAV is shorter than one column."""
    hop = rate // params.columns_per_second
    if hop <= 0 or len(samples) < hop:
        return None
    scaled, columns = strip(samples, rate, params.n_fft, hop, params.bands, params.f_min, params.f_max, params.db_floor)
    png.parent.mkdir(parents=True, exist_ok=True)
    write_png(png, colormap(scaled))
    return {
        "fields": {
            "sample_rate_hz": rate,
            "n_fft": params.n_fft,
            "hop_frames": hop,
            "n_bands": params.bands,
            "width_px": columns,
            "height_px": params.bands,
            "duration_ms": ms_from_frames(len(samples), rate),
            "covers_ms": min(ms_from_frames(columns * hop, rate), ms_from_frames(len(samples), rate)),
            "db_floor": params.db_floor,
            "colormap": "ga-ember",
        },
        "params": {
            "f_min_hz": params.f_min,
            "f_max_hz": min(params.f_max, rate / 2),
            "band_spacing": "geometric",
            "seconds_per_px": hop / rate,
            "window": "hann",
            "columns_per_second": params.columns_per_second,
        },
    }


def write_cube_png(path: Path, points: list[dict], size: tuple[int, int] = CUBE_PNG_SIZE) -> None:
    """Small isometric raster of the cube's ``points_preview`` (the points the cube viewport draws).

    Same axes as the viewport: time to the right, frequency up, the four layers
    stacked in depth, layer colors from :data:`gen_audio.cube_layers.LAYER_COLORS`.
    Written with zlib (no matplotlib), so it stays small per voice.
    """
    width, height = size
    image = np.zeros((height, width, 3), dtype=np.float64)
    image[:] = (6, 9, 15)
    index = {name: i for i, name in enumerate(LAYER_NAMES)}
    # Back layers first so the front (signal) layer paints over them.
    for point in sorted(points, key=lambda item: -index.get(item.get("layer"), 0)):
        layer = index.get(point.get("layer"))
        if layer is None:
            continue
        depth = layer / 3.0
        px = int(round(36 + float(point["t"]) * (width - 150) + depth * 96))
        py = int(round(height - 28 - float(point["f"]) * (height - 130) - depth * 72))
        value = min(1.0, max(0.0, float(point.get("v", 0.0))))
        color = np.asarray(LAYER_COLORS[layer], dtype=np.float64) * 255.0 * (0.35 + 0.65 * value)
        image[max(0, py - 1) : py + 2, max(0, px - 1) : px + 2] = color
    write_png(path, np.clip(image, 0, 255).astype(np.uint8))


def _honesty_real() -> dict:
    return {
        "synthesized_speech": False,
        "fixture": False,
        "claims": ["real_wav"],
        "note": "Combined excerpts of a real recording, cut by Yumlog Studio speaker detection. Not synthesized speech. The alias is a label, not a person's name.",
    }


def import_set(source_dir: Path | str, set_name: str, library: Path | str = LIBRARY) -> dict:
    """Import one Yumlog AgentVoice set. Returns ``{"voices": [...], "assets": [...], "source": {...}}``.

    WAVs are copied to ``<library>/agentvoice/<set>/<alias>.wav`` (gitignored);
    strips and cubes are written beside them.
    """
    if not _SET.match(set_name):
        raise AgentVoiceSourceError(f"set name {set_name!r} must match {_SET.pattern}")
    source_dir = Path(source_dir)
    library = Path(library)
    source = load_source(source_dir / "agent-voices.json")
    out_dir = library / CATALOG_DIR / set_name
    out_dir.mkdir(parents=True, exist_ok=True)
    voices: list[dict] = []
    assets: list[dict] = []
    for voice in source.voices:
        legacy = f"agentvoice.{set_name}.{voice.alias}"
        appearances = [{"start_s": start, "end_s": end} for start, end in voice.turns]
        extension = {
            "schema": YUMLOG_SCHEMA,
            "set": set_name,
            "speaker_id": voice.speaker_id,
            "alias": voice.alias,
            "appearances": appearances,
            "appearance_count": len(appearances),
            "total_s": voice.duration_s,
            "recording_sha256": source.recording_sha256,
            "recording_duration_s": source.recording_duration_s,
            "method": source.method,
        }
        row = {
            "set": set_name,
            "alias": voice.alias,
            "speaker_id": voice.speaker_id,
            "legacy_id": legacy,
            "appearance_count": len(appearances),
            "total_s": voice.duration_s,
            "appearances": appearances,
            "status": "unavailable",
        }
        source_wav = source_dir / voice.wav_name
        if not source_wav.is_file():
            row["reason"] = "Yumlog listed this voice, but its WAV is not in the imported folder."
            voices.append(row)
            continue
        if _sha256(source_wav) != voice.sha256:
            row["reason"] = "The WAV does not match the sha256 Yumlog recorded, so it was not imported."
            voices.append(row)
            continue
        wav_rel = f"{CATALOG_DIR}/{set_name}/{voice.alias}.wav"
        target = library / wav_rel
        if source_wav.resolve() != target.resolve():
            shutil.copyfile(source_wav, target)
        samples, rate = read_wav(target)
        frames = len(samples)
        wav_media = _media(library, wav_rel, "wav", "audio/wav")
        info = _wav_header(target)
        clip_fields = {"engine": ENGINE, "sample_rate_hz": rate, "duration_ms": ms_from_frames(frames, rate)}
        if info.get("channels"):
            clip_fields["channels"] = info["channels"]
        segments, cursor, wav_s = [], 0.0, frames / rate
        for start, end in voice.turns:
            span = end - start
            seg_start, seg_end = round(min(cursor, wav_s), 6), round(min(cursor + span, wav_s), 6)
            if seg_end > seg_start:
                segments.append({"speaker_idx": 0, "start_s": seg_start, "end_s": seg_end})
            cursor += span
        clip = envelope(
            kind="audio_clip",
            legacy_id=legacy,
            title=voice.alias,
            summary=f"{len(appearances)} appearance{'s' if len(appearances) != 1 else ''} \u00b7 {_clock(voice.duration_s)} total. Yumlog Studio AgentVoice, combined source excerpts.",
            status="ok",
            fields=clip_fields,
            media=[wav_media],
            src=[],
            honesty=_honesty_real(),
            provenance={
                "generator": GENERATOR,
                "engine": ENGINE,
                "params": {
                    key: value
                    for key, value in {
                        "yumlog_schema": YUMLOG_SCHEMA,
                        "diarization_method": source.method,
                        "speaker_id": voice.speaker_id,
                        "yumlog_artifact_id": voice.artifact_id,
                        "recording_sha256": source.recording_sha256,
                    }.items()
                    if value
                },
            },
            body={
                "duration_s": frames / rate,
                "sample_rate": rate,
                # One diarized speaker per WAV: segments are that speaker's turns,
                # laid end to end in the combined excerpt.
                "speakers": [{"idx": 0, "persona": voice.alias, "voice": voice.speaker_id or voice.alias, "engine": ENGINE}],
                "segments": segments,
                "wav_url": f"/library/{wav_rel}",
                "n_turns": len(appearances),
            },
            extensions={EXTENSION: extension},
        )
        assets.append(clip)
        row.update({"status": "ok", "clip_uid": clip["uid"]})

        params = strip_params(frames / rate)
        spec_rel = f"{CATALOG_DIR}/{set_name}/{voice.alias}_spec2d.png"
        spec = write_spectrogram(samples, rate, library / spec_rel, params)
        if spec is None:
            row["spectrogram_reason"] = "The WAV is shorter than one strip column."
        else:
            spectrogram = envelope(
                kind="spectrogram_2d",
                legacy_id=f"{legacy}.spec2d",
                title=f"2D spectrogram \u2014 {voice.alias}",
                summary=None,
                status="ok",
                fields={"source_sha256": wav_media["sha256"], **spec["fields"]},
                media=[_media(library, spec_rel, "spectrogram_png", "image/png")],
                src=[clip["uid"]],
                honesty={"synthesized_speech": False, "fixture": False, "claims": ["library_spectrogram"]},
                provenance={"generator": "src/gen_audio/spectrogram_strip.py", "params": spec["params"]},
                body={"seconds_per_px": spec["params"]["seconds_per_px"]},
            )
            assets.append(spectrogram)
            row["spectrogram_uid"] = spectrogram["uid"]

        stem = f"{CATALOG_DIR}/{set_name}/{voice.alias}"
        doc, _cloud = library_cube(samples, rate, stem=stem, engine=ENGINE, source_sha256=wav_media["sha256"], label=voice.alias)
        doc["source_wav"] = wav_rel
        doc["wavUrl"] = f"/library/{wav_rel}"
        doc["pngUrl"] = f"/library/{stem}_cube3d.png"
        cube_json_rel, cube_png_rel = f"{stem}_cube3d.json", f"{stem}_cube3d.png"
        (library / cube_json_rel).write_bytes((json.dumps(doc, separators=(",", ":")) + "\n").encode("utf-8"))
        write_cube_png(library / cube_png_rel, doc["points_preview"])
        cube = _cube_envelope(library, legacy, voice.alias, doc, clip["uid"], wav_media["sha256"], cube_json_rel, cube_png_rel)
        assets.append(cube)
        row["cube_uid"] = cube["uid"]
        for index, name in enumerate(LAYER_NAMES):
            stats = doc["layers"].get(name)
            if stats is None:
                continue
            assets.append(
                envelope(
                    kind="layer",
                    legacy_id=f"{legacy}.cube.{name}",
                    title=f"{name} layer",
                    summary=None,
                    status="ok",
                    fields={"name": name, "index": index},
                    media=[],
                    src=[cube["uid"]],
                    relations={"layer_of": cube["uid"]},
                    honesty={"synthesized_speech": False, "fixture": False, "claims": ["library_cube"]},
                    provenance={"generator": "cube JSON layers block"},
                    body=stats,
                )
            )
        voices.append(row)
    return {
        "source": {
            "set": set_name,
            "schema": YUMLOG_SCHEMA,
            "method": source.method,
            "recording_sha256": source.recording_sha256,
            "recording_duration_s": source.recording_duration_s,
            "voice_count": len(source.voices),
        },
        "voices": voices,
        "assets": assets,
    }


def _cube_envelope(library: Path, legacy: str, alias: str, doc: dict, clip_uid: str, wav_sha: str, json_rel: str, png_rel: str) -> dict:
    """cube_ihdr fields as Rust ``asset_migrate::cube_identity`` derives them from a cube JSON."""
    rate = int(doc["sample_rate"])
    freq_bins, time_bins = (int(n) for n in doc["cube_shape_f_t"])
    bin_frames = int(doc["downsample_sf_st"][1]) * int(doc["hop"])
    duration_ms = round_half_up(float(doc["duration_s"]) * 1000.0)
    # The last time bin is padded past the end of a very short WAV (cube_covers_s
    # can exceed duration_s by part of a bin); coverage of the audio itself is at
    # most the audio, which is what the v1 rule covers_ms <= duration_ms asks.
    covers_ms = min(round_half_up(float(doc["cube_covers_s"]) * 1000.0), duration_ms)
    fields = {
        "source_sha256": wav_sha,
        "sample_rate_hz": rate,
        "bin_frames": bin_frames,
        "time_bins": max(1, time_bins),
        "freq_bins": freq_bins,
        "duration_ms": duration_ms,
        "covers_ms": covers_ms,
        "inv_hdr_ppm": round_half_up(float(doc["inv_hdr"]) * 1_000_000.0),
        "cube_revision": int(doc["cube_revision"]),
        "n_points": int(doc["n_points"]),
        "n_fft": int(doc["n_fft"]),
        "hop_frames": int(doc["hop"]),
        "generator_sha256": doc["provenance"]["generator_sha256"],
        "layer_method": doc["provenance"]["layer_method"],
    }
    provenance = dict(doc["provenance"])
    provenance["params"] = {**provenance.get("params", {}), "bins_inferred_from_shape": False}
    return envelope(
        kind="cube_ihdr",
        legacy_id=f"{legacy}.cube",
        title=doc["title"],
        summary=f"Inverse-HDR cube rev {doc['cube_revision']} over the full {doc['duration_s']:.3f} s AgentVoice WAV. Scrub maps by seconds: bin = floor(t / {bin_frames / rate:.6f} s).",
        status="ok",
        fields=fields,
        media=[_media(library, json_rel, "cube_json", "application/json"), _media(library, png_rel, "cube_png", "image/png")],
        src=[clip_uid],
        honesty={"synthesized_speech": False, "fixture": False, "claims": ["library_cube"]},
        provenance=provenance,
        body={
            "inv_hdr": doc["inv_hdr"],
            "layer_score": doc["layer_score"],
            "duration_s": doc["duration_s"],
            "speakers": "unresolved",
            "bin_seconds": bin_frames / rate,
            "cube_covers_s": covers_ms / 1000.0,
            "cube_shape_f_t": [freq_bins, max(1, time_bins)],
        },
    )


def _wav_header(path: Path) -> dict:
    """channels / sample_rate / frames from the RIFF header (build_assets ``wav_facts``)."""
    data = path.read_bytes()[:4096]
    out: dict = {}
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return out
    offset = 12
    block = 0
    while offset + 8 <= len(data):
        tag = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        body = offset + 8
        if tag == b"fmt " and body + 16 <= len(data):
            out["channels"] = int.from_bytes(data[body + 2 : body + 4], "little")
            out["sample_rate"] = int.from_bytes(data[body + 4 : body + 8], "little")
            block = int.from_bytes(data[body + 12 : body + 14], "little")
        elif tag == b"data" and block:
            out["frames"] = size // block
            return out
        offset = body + size + (size & 1)
    return out


def write_catalog(imported: list[dict], library: Path | str = LIBRARY, keep_existing: bool = True) -> Path:
    """Merge imported sets into ``<library>/agentvoice/assets.json`` (a set replaces its old rows)."""
    library = Path(library)
    path = library / CATALOG_DIR / CATALOG_NAME
    sets: dict[str, dict] = {}
    if keep_existing and path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        for source in existing.get("sources", []):
            name = source["set"]
            sets[name] = {
                "source": source,
                "voices": [row for row in existing.get("voices", []) if row.get("set") == name],
                "assets": [asset for asset in existing.get("assets", []) if asset.get("legacy_id", "").startswith(f"agentvoice.{name}.")],
            }
    for one in imported:
        sets[one["source"]["set"]] = one
    ordered = [sets[name] for name in sorted(sets)]
    doc = {
        "schema_version": SCHEMA_VERSION,
        "note": "Yumlog Studio AgentVoice sets imported by `python -m gen_audio.cli.agent_voices import`. One row in voices per source voice; audio-backed voices carry an audio_clip, its spectrogram_2d and its rev 3 cube_ihdr. WAVs are gitignored.",
        "sources": [one["source"] for one in ordered],
        "voices": [row for one in ordered for row in one["voices"]],
        "assets": [asset for one in ordered for asset in one["assets"]],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return path


def check_catalog(doc: dict, library: Path | str = LIBRARY, require_wavs: bool = False) -> list[str]:
    """Problems with a catalog: counts, uids that do not re-mint, committed media that drifted."""
    library = Path(library)
    problems: list[str] = []
    by_uid = {asset["uid"]: asset for asset in doc.get("assets", [])}
    for source in doc.get("sources", []):
        rows = [row for row in doc.get("voices", []) if row.get("set") == source["set"]]
        if len(rows) != source.get("voice_count"):
            problems.append(f"{source['set']}: {len(rows)} voice rows for {source.get('voice_count')} source voices")
    for asset in doc.get("assets", []):
        digests = [{"role": item["role"], "sha256": item["sha256"]} for item in asset["media"]]
        if mint(asset["kind"], asset["fields"], digests, asset["src"]) != asset["uid"]:
            problems.append(f"{asset['uid']} does not re-mint from its fields")
        if asset["display"].get("glyph") != glyph_from_uid(asset["uid"]):
            problems.append(f"{asset['uid']} glyph is not its digest glyph")
        for item in asset["media"]:
            path = library / item["path"]
            if not path.is_file():
                if item["role"] != "wav" or require_wavs:
                    problems.append(f"{item['path']} is missing")
                continue
            if item["role"] != "wav" or require_wavs:
                if path.stat().st_size != item["bytes"] or _sha256(path) != item["sha256"]:
                    problems.append(f"{item['path']} differs from its catalog sha256")
    for row in doc.get("voices", []):
        if row["status"] != "ok":
            if row.get("clip_uid"):
                problems.append(f"{row['legacy_id']} is unavailable but names a clip")
            continue
        for key, kind in (("clip_uid", "audio_clip"), ("spectrogram_uid", "spectrogram_2d"), ("cube_uid", "cube_ihdr")):
            uid = row.get(key)
            if key == "spectrogram_uid" and uid is None and row.get("spectrogram_reason"):
                continue
            if not uid or by_uid.get(uid, {}).get("kind") != kind:
                problems.append(f"{row['legacy_id']} has no {kind}")
    return problems
