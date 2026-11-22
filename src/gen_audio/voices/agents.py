"""Agent voice blends: ``voices/agents/<name>.json``, listed in ``voices/agents/catalog.json``.

A persona in ``crates/gen-audio-core/src/catalog.rs`` names its blend with a
``voice-blend:<name>`` ref, and the cast map carries ``<name>`` as the voice.
Both Kokoro engines speak it: kokoro_onnx mixes the style vectors from
``voices-v1.0.bin`` and kokoro_dayour mixes the same hexgrad/Kokoro-82M
``.pt`` packs. Each phonemizes with the blend's Misaki config.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from gen_audio.voices.errors import VoiceError

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")


def agents_dir() -> Path:
    root = Path(os.environ["GEN_AUDIO_REPO"]) if os.environ.get("GEN_AUDIO_REPO") else Path(__file__).resolve().parents[3]
    return root / "voices" / "agents"


def agent_voice(name: object) -> dict | None:
    """The blend document for ``name``, or ``None`` when ``name`` is not a listed agent voice."""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        return None
    folder = agents_dir()
    catalog = folder / "catalog.json"
    if not catalog.is_file():
        return None
    listed = json.loads(catalog.read_text(encoding="utf-8")).get("voices", [])
    if f"{name}.json" not in listed:
        return None
    voice = json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))
    parts = blend_parts(voice)
    total = sum(weight for _, weight in parts)
    if abs(total - 1.0) > 1e-6:
        raise VoiceError(f"agent voice {name} blend weights sum to {total}, not 1", code="bad_blend")
    return voice


def blend_parts(voice: dict) -> list[tuple[str, float]]:
    blend = (voice.get("params") or {}).get("blend")
    if not isinstance(blend, list) or len(blend) < 2:
        raise VoiceError(f"agent voice {voice.get('id')} needs a blend of two or more packs", code="bad_blend")
    return [(str(part["voice_id"]), float(part["weight"])) for part in blend]


def agent_g2p(voice: dict):
    """The Misaki config the blend names: ``voices/agents/g2p`` first, then the shipped ``voices/g2p``."""
    from gen_audio.voices.model import g2p_config_path, load_g2p

    g2p_id = voice.get("g2p")
    if not isinstance(g2p_id, str) or not g2p_id:
        raise VoiceError(f"agent voice {voice.get('id')} names no g2p config", code="missing_misaki")
    local = agents_dir() / "g2p" / f"{g2p_id}.json"
    return load_g2p(local if local.is_file() else g2p_config_path(g2p_id))