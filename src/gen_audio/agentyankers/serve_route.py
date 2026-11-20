"""Serve helper for an AgentYankers episode.

``POST /genaid-audio/agentyankers/episode`` dry-runs or renders a rundown.
The handler writes files only under ``GEN_AUDIO_ARTIFACT_DIR``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from gen_audio.agentyankers.contract import (
    SCHEMA_VERSION,
    AgentYankersError,
    ConsentRefusal,
    CoreUnavailable,
    MissingRecording,
    PhoneNumberRefusal,
    VibeVoiceRuntimeUnavailable,
    assert_no_phone_numbers,
    recording_disclosure,
    reject_inline_secrets,
)
from gen_audio.agentyankers.core import core_status
from gen_audio.agentyankers.episode import render_episode
from gen_audio.audio_io import read_wav
from gen_audio.guards import safe_storage_name
from gen_audio.serve import AGENTYANKERS_EPISODE_PATH, ROUTE_PREFIX

EPISODE_ROUTE = f"{ROUTE_PREFIX}{AGENTYANKERS_EPISODE_PATH}"
MAX_BODY_BYTES = 262_144


class BodyError(Exception):
    """The HTTP body cannot be read as an episode request."""

    def __init__(self, status: int, payload: dict[str, Any]):
        super().__init__(payload.get("message", "invalid body"))
        self.status = status
        self.payload = payload


def format_descriptor() -> dict[str, Any]:
    """Describe the route. This body has no episode audio."""
    return {
        "service": "genaid-audio",
        "format": "agentyankers",
        "schema_version": SCHEMA_VERSION,
        "route": EPISODE_ROUTE,
        "modes": ["dry_run", "render"],
        "core": core_status(),
        "speech_synthesized": False,
        "disclosure": recording_disclosure(),
    }


def parse_request_body(raw: bytes, content_type: str | None, content_length: str | None) -> dict[str, Any]:
    """Decode one JSON object. Raises ``BodyError`` with an HTTP status."""
    if content_length is None:
        raise BodyError(411, {"error": "length_required", "message": "Content-Length is required"})
    try:
        size = int(content_length)
    except (TypeError, ValueError) as exc:
        raise BodyError(400, {"error": "invalid_body", "message": "Content-Length must be an integer"}) from exc
    if size < 0 or size > MAX_BODY_BYTES or len(raw) > MAX_BODY_BYTES:
        raise BodyError(
            413,
            {"error": "body_too_large", "message": f"body must be at most {MAX_BODY_BYTES} bytes"},
        )
    if len(raw) != size:
        raise BodyError(400, {"error": "invalid_body", "message": "Content-Length does not match the body"})
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise BodyError(415, {"error": "unsupported_media", "message": "Content-Type must be application/json"})
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BodyError(400, {"error": "invalid_body", "message": "body must be UTF-8 JSON"}) from exc
    import json

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BodyError(400, {"error": "invalid_body", "message": "body must be JSON"}) from exc
    if not isinstance(payload, dict):
        raise BodyError(400, {"error": "invalid_body", "message": "episode body must be a JSON object"})
    return payload


def handle_episode(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Dry-run or render the rundown in ``body``."""
    try:
        reject_inline_secrets(body)
        assert_no_phone_numbers(body, field="payload")
        if "request" in body:
            raise AgentYankersError(
                "This route renders an AgentYankers rundown. "
                "EpisodePipeline.assemble accepts an EpisodeRequest in process "
                "when genaid_audio.agentyankers is installed."
            )
        mode = body.get("mode", "dry_run")
        if mode not in {"dry_run", "render"}:
            raise AgentYankersError("mode must be dry_run or render")
        if "rundown" not in body or "marks" not in body:
            raise AgentYankersError("episode body needs rundown and marks")
        output = _output_dir(body.get("output_dir"))
        recordings = _recordings(body.get("recordings"), output)
        seed = body.get("seed", 0)
        logline = body.get("logline", "")
        if not isinstance(logline, str):
            raise AgentYankersError("logline must be a string")
        rendered = render_episode(
            body["rundown"],
            body["marks"],
            mode=mode,
            output_dir=output,
            recordings=recordings,
            seed=seed,
            logline=logline,
        )
    except BodyError:
        raise
    except PhoneNumberRefusal as exc:
        return 409, {"error": exc.code, "message": str(exc), "speech_synthesized": False}
    except ConsentRefusal as exc:
        return 409, {
            "error": exc.code,
            "message": str(exc),
            "mark_id": exc.mark_id,
            "speech_synthesized": False,
        }
    except MissingRecording as exc:
        return 409, {"error": exc.code, "message": str(exc), "speech_synthesized": False}
    except (VibeVoiceRuntimeUnavailable, CoreUnavailable) as exc:
        return 503, {"error": exc.code, "message": str(exc), "speech_synthesized": False}
    except AgentYankersError as exc:
        return 400, {"error": exc.code, "message": str(exc), "speech_synthesized": False}
    except Exception as exc:
        from gen_audio.adapters import EngineRefusal

        if isinstance(exc, EngineRefusal):
            return 409, {
                "error": "vibevoice_unavailable",
                "message": str(exc),
                "missing": list(exc.missing),
                "speech_synthesized": False,
            }
        raise
    return 200, rendered.public_body()


def _output_dir(requested: Any) -> Path | None:
    if requested is None:
        return None
    if not isinstance(requested, str) or not requested.strip():
        raise AgentYankersError("output_dir must be a relative directory under GEN_AUDIO_ARTIFACT_DIR")
    root = os.environ.get("GEN_AUDIO_ARTIFACT_DIR", "").strip()
    if not root:
        raise AgentYankersError("GEN_AUDIO_ARTIFACT_DIR is unset")
    if "\x00" in requested or requested.startswith(("/", "\\")) or ":" in requested:
        raise AgentYankersError("output_dir must stay under GEN_AUDIO_ARTIFACT_DIR")
    parts = requested.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AgentYankersError("output_dir must stay under GEN_AUDIO_ARTIFACT_DIR")
    destination = Path(root).joinpath(*parts).resolve()
    root_real = Path(root).resolve()
    if destination != root_real and root_real not in destination.parents:
        raise AgentYankersError("output_dir must stay under GEN_AUDIO_ARTIFACT_DIR")
    return destination


def _recordings(payload: Any, output: Path | None) -> dict[str, np.ndarray]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise AgentYankersError("recordings must be an object of ref id to wav file name")
    if output is None:
        raise AgentYankersError("recordings need output_dir so the wavs stay in the artifact directory")
    root = Path(os.environ.get("GEN_AUDIO_ARTIFACT_DIR", "")).resolve()
    loaded: dict[str, np.ndarray] = {}
    for ref, name in payload.items():
        if not isinstance(ref, str) or not isinstance(name, str):
            raise AgentYankersError("recordings map a ref id to a wav file name")
        try:
            filename = safe_storage_name(name)
        except ValueError as exc:
            raise AgentYankersError("recordings must name a .wav file in the artifact directory") from exc
        if not filename.lower().endswith(".wav"):
            raise AgentYankersError("recordings must name a .wav file in the artifact directory")
        path = (root / filename).resolve()
        if root != path and root not in path.parents:
            raise AgentYankersError("recording must stay under GEN_AUDIO_ARTIFACT_DIR")
        if not path.is_file():
            raise MissingRecording(f"no audio stored for {ref}")
        audio, rate = read_wav(path)
        if int(rate) != 24_000:
            raise AgentYankersError("recordings use 24000 Hz")
        loaded[ref] = audio
    return loaded
