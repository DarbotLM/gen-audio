"""Read the complete local DarbotLM/genaid-voices catalog without network access."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_MAX_CATALOG_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class GenaidVoice:
    voice_id: str
    name: str
    language: str
    locale: str
    gender: str
    sample_file: str
    sample_rate: int
    duration_seconds: float
    profile_file: Path | None = None


class GenaidVoiceCatalog:
    """Validated view over the shared GENAID voice-library checkout."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        path = self.root / "voice-catalog.json"
        if not path.is_file():
            raise ValueError(f"GENAID voice catalog is missing: {path}")
        raw = path.read_bytes()
        if len(raw) > _MAX_CATALOG_BYTES:
            raise ValueError("GENAID voice catalog exceeds 4 MiB")
        payload = json.loads(raw)
        if payload.get("version") != "1.0.0" or not isinstance(payload.get("voices"), list):
            raise ValueError("Unsupported GENAID voice catalog")
        voices: list[GenaidVoice] = []
        seen: set[str] = set()
        for row in payload["voices"]:
            if not isinstance(row, dict):
                raise ValueError("GENAID voice row must be an object")  # noqa: TRY004
            voice_id = row.get("id")
            analysis = row.get("analysis") or {}
            sample = row.get("sample_file")
            if not isinstance(voice_id, str) or not voice_id or voice_id in seen:
                raise ValueError("GENAID voice ids must be unique strings")
            if not isinstance(sample, str) or Path(sample).is_absolute() or ".." in Path(sample).parts:
                raise ValueError(f"GENAID voice {voice_id} has an invalid sample_file")
            sample_path = (self.root / sample).resolve()
            if sample_path != self.root and self.root not in sample_path.parents:
                raise ValueError(f"GENAID voice {voice_id} escapes the library")
            profile = self.root / "profiles" / f"{voice_id}.json"
            voices.append(
                GenaidVoice(
                    voice_id=voice_id,
                    name=str(row.get("name") or voice_id),
                    language=str(row.get("language") or ""),
                    locale=str(row.get("locale") or ""),
                    gender=str(row.get("gender") or ""),
                    sample_file=sample,
                    sample_rate=int(analysis.get("sample_rate") or 0),
                    duration_seconds=float(analysis.get("duration_seconds") or 0),
                    profile_file=profile if profile.is_file() else None,
                )
            )
            seen.add(voice_id)
        if not voices:
            raise ValueError("GENAID voice catalog is empty")
        self.voices = tuple(voices)
        self.by_id = {voice.voice_id: voice for voice in voices}

    def get(self, voice_id: str) -> GenaidVoice:
        try:
            return self.by_id[voice_id]
        except KeyError as exc:
            raise ValueError(f"Unknown GENAID voice: {voice_id}") from exc

    def sample_path(self, voice_id: str, *, require: bool = True) -> Path:
        path = self.root / self.get(voice_id).sample_file
        if require and not path.is_file():
            raise ValueError(f"GENAID voice sample is missing: {path}")
        return path.resolve()


def configured_catalog(explicit: Path | str | None = None) -> GenaidVoiceCatalog:
    raw = str(explicit).strip() if explicit is not None else ""
    raw = raw or os.environ.get("GENAID_VOICES_DIR", "").strip()
    if not raw:
        raise ValueError("GENAID_VOICES_DIR is unset")
    root = Path(raw)
    if not root.is_dir():
        raise ValueError(f"GENAID_VOICES_DIR is not a directory: {root}")
    return GenaidVoiceCatalog(root)


def resolve_sample(voice_id: str, *, root: Path | str | None = None) -> Path:
    return configured_catalog(root).sample_path(voice_id)

