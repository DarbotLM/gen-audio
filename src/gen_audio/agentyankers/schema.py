"""Validate the published ``agentyankers/v1`` rundown shape.

The JSON document in ``gen_audio/data/schemas/agentyankers.v1.schema.json`` is
the shape other public callers can check. When ``genaid_audio.agentyankers``
is installed, ``export_json_schemas`` returns that package's schemas instead.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from gen_audio.agentyankers.contract import (
    PRODUCER,
    RECORDING_DISCLOSURE,
    SCHEMA_VERSION,
    SHOW_NAME,
    AgentYankersError,
    assert_no_phone_numbers,
)

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "data" / "schemas" / "agentyankers.v1.schema.json"

_EPISODE_ID = re.compile(r"^ep-[a-z0-9-]{3,64}$")
_PREMISE_ID = re.compile(r"^prem-[a-z0-9-]{3,64}$")
_SEG_ID = re.compile(r"^seg-[a-z0-9-]{2,64}$")
_CUE_ID = re.compile(r"^cue-[a-z0-9-]{2,64}$")
_CHAPTER_ID = re.compile(r"^ch-[a-z0-9-]{2,64}$")
_MARK_ID = re.compile(r"^mark-[a-z0-9-]{3,64}$")
_CONSENT_ID = re.compile(r"^cons-[a-z0-9-]{3,64}$")
_CONTACT = re.compile(r"^(agent|lk-identity|room):[a-z0-9][a-z0-9-]{2,64}$")
_VOICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,80}$")
_AGENT = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,31}$")
_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TAG = re.compile(r"^[a-z][a-z0-9-]{0,24}$")
_REC_ID = re.compile(r"^rec-[a-z0-9-]{3,80}$")

_SEGMENT_KINDS = {"stinger", "disclosure", "commentary", "call_clip", "laugh", "outro"}
_CUE_KINDS = {"laugh", "stinger", "bed", "transition"}
_PLACEMENTS = {"before", "after", "under"}
_LIBRARIES = {"genaid-voices", "dlm-voicemapper", "personaplex-prompt"}
_PROVIDERS = {"vibevoice", "personaplex"}
_SCOPES = {"none", "call", "publish", "call_and_publish"}
_MARK_KINDS = {"agent", "person"}

_RUNDOWN_KEYS = {
    "schema_version",
    "episode_id",
    "show_name",
    "producer",
    "title",
    "premise_id",
    "segments",
    "cues",
    "chapters",
    "disclosure_text",
}
_SEGMENT_KEYS = {
    "schema_version",
    "segment_id",
    "kind",
    "title",
    "text",
    "voice",
    "recording_ref_id",
    "start_seconds",
    "duration_seconds",
    "source_clip_id",
    "source_session_id",
    "chapter_title",
}
_VOICE_KEYS = {"schema_version", "voice_id", "library", "provider", "relative_path"}
_CUE_KEYS = {
    "schema_version",
    "cue_id",
    "kind",
    "placement",
    "anchor_id",
    "asset_ref",
    "gain_db",
    "duration_seconds",
}
_CHAPTER_KEYS = {"schema_version", "chapter_id", "title", "start_seconds", "end_seconds"}
_MARK_KEYS = {
    "schema_version",
    "mark_id",
    "display_name",
    "kind",
    "opted_in",
    "consent",
    "contact_ref",
    "notes",
    "tags",
}
_CONSENT_KEYS = {
    "schema_version",
    "opted_in",
    "consent_id",
    "scope",
    "disclosure_accepted",
    "consented_at",
}


def published_schema_document() -> dict[str, Any]:
    """The schema file shipped with this package."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def export_json_schemas() -> dict[str, Any]:
    """Core schemas when installed, otherwise the published rundown shape."""
    from gen_audio.agentyankers.core import core_export_json_schemas

    exported = core_export_json_schemas()
    if exported is not None:
        return exported
    document = published_schema_document()
    defs = document.get("$defs")
    if not isinstance(defs, dict) or "EpisodeRundown" not in defs:
        raise AgentYankersError("published AgentYankers schema is missing EpisodeRundown")
    return defs


def validate_rundown(payload: Any) -> dict[str, Any]:
    """Return the rundown when it matches ``agentyankers/v1``."""
    if not isinstance(payload, dict):
        raise AgentYankersError("rundown must be an object")
    _exact_keys(payload, _RUNDOWN_KEYS, required=_RUNDOWN_KEYS, label="rundown")
    _version(payload)
    _match(_EPISODE_ID, payload["episode_id"], "episode_id")
    _match(_PREMISE_ID, payload["premise_id"], "premise_id")
    if payload["show_name"] != SHOW_NAME or payload["producer"] != PRODUCER:
        raise AgentYankersError("rundown show_name and producer are fixed by the format")
    title = _text(payload["title"], "title", 120)
    payload = dict(payload)
    payload["title"] = title
    if payload["disclosure_text"] != RECORDING_DISCLOSURE:
        raise AgentYankersError("episode disclosure text cannot be replaced")
    segments = payload["segments"]
    if not isinstance(segments, list) or not segments or len(segments) > 64:
        raise AgentYankersError("rundown segments must be a list of 1 to 64 items")
    cues = payload["cues"]
    chapters = payload["chapters"]
    if not isinstance(cues, list) or len(cues) > 64:
        raise AgentYankersError("rundown cues must be a list of at most 64 items")
    if not isinstance(chapters, list) or len(chapters) > 64:
        raise AgentYankersError("rundown chapters must be a list of at most 64 items")
    payload["segments"] = [_segment(item, index) for index, item in enumerate(segments)]
    payload["cues"] = [_cue(item, index) for index, item in enumerate(cues)]
    payload["chapters"] = [_chapter(item, index) for index, item in enumerate(chapters)]
    kinds = [item["kind"] for item in payload["segments"]]
    if "disclosure" not in kinds:
        raise AgentYankersError("episode rundown is missing the recording disclosure")
    assert_no_phone_numbers(payload, field="rundown")
    return payload


def validate_mark(payload: Any) -> dict[str, Any]:
    """Return one mark index entry."""
    if not isinstance(payload, dict):
        raise AgentYankersError("mark must be an object")
    _exact_keys(
        payload,
        _MARK_KEYS,
        required=_MARK_KEYS - {"notes", "tags"},
        label="mark",
    )
    _version(payload)
    mark = dict(payload)
    _match(_MARK_ID, mark["mark_id"], "mark_id")
    mark["display_name"] = _text(mark["display_name"], "display_name", 80)
    if mark["kind"] not in _MARK_KINDS:
        raise AgentYankersError("mark kind must be agent or person")
    if not isinstance(mark["opted_in"], bool):
        raise AgentYankersError("mark opted_in must be a boolean")
    _match(_CONTACT, mark["contact_ref"], "contact_ref")
    notes = mark.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 400:
        raise AgentYankersError("mark notes must be a string of at most 400 characters")
    mark["notes"] = notes
    tags = mark.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 12:
        raise AgentYankersError("mark tags must be a list of at most 12 items")
    mark["tags"] = _tags(tags)
    mark["consent"] = _consent(mark["consent"])
    if mark["opted_in"] != mark["consent"]["opted_in"]:
        raise AgentYankersError("opted_in must match consent.opted_in")
    if not mark["opted_in"] and mark["consent"]["scope"] != "none":
        raise AgentYankersError("a mark who has not opted in must use consent scope none")
    if mark["opted_in"] and mark["consent"]["scope"] == "none":
        raise AgentYankersError("an opted-in mark needs a call, publish, or call_and_publish scope")
    if mark["opted_in"] and not mark["consent"]["disclosure_accepted"]:
        raise AgentYankersError("opted-in marks must accept the recording disclosure")
    assert_no_phone_numbers(mark, field="mark")
    return mark


def validate_marks(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload or len(payload) > 64:
        raise AgentYankersError("marks must be a list of 1 to 64 entries")
    marks = [validate_mark(item) for item in payload]
    ids = [mark["mark_id"] for mark in marks]
    if len(ids) != len(set(ids)):
        raise AgentYankersError("duplicate mark_id")
    return marks


def _segment(payload: Any, index: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentYankersError(f"segments[{index}] must be an object")
    _exact_keys(
        payload,
        _SEGMENT_KEYS,
        required={"schema_version", "segment_id", "kind", "title"},
        label=f"segments[{index}]",
    )
    _version(payload)
    item = dict(payload)
    _match(_SEG_ID, item["segment_id"], "segment_id")
    if item["kind"] not in _SEGMENT_KINDS:
        raise AgentYankersError(f"segments[{index}] kind is not a rundown segment")
    item["title"] = _text(item["title"], "title", 120)
    text = item.get("text")
    if text is not None:
        item["text"] = _text(text, "text", 4000)
    if item["kind"] == "disclosure":
        spoken = item.get("text", RECORDING_DISCLOSURE)
        if spoken != RECORDING_DISCLOSURE:
            raise AgentYankersError("episode disclosure text cannot be replaced")
        item["text"] = RECORDING_DISCLOSURE
    if item["kind"] in {"disclosure", "commentary"}:
        if item["kind"] == "commentary" and not item.get("text"):
            raise AgentYankersError(f"segments[{index}] commentary needs text")
        item["voice"] = _voice(item.get("voice"), index)
        if item["voice"]["provider"] != "vibevoice":
            raise AgentYankersError("host commentary uses a vibevoice voice from genaid-voices")
    elif "voice" in item and item["voice"] is not None:
        item["voice"] = _voice(item["voice"], index)
    if item["kind"] == "call_clip":
        _match(_REC_ID, item.get("recording_ref_id"), "recording_ref_id")
        _duration(item.get("duration_seconds"), f"segments[{index}].duration_seconds", required=True)
    else:
        ref = item.get("recording_ref_id")
        if ref is not None:
            _match(_REC_ID, ref, "recording_ref_id")
        if "duration_seconds" in item and item["duration_seconds"] is not None:
            _duration(item["duration_seconds"], f"segments[{index}].duration_seconds", required=False)
    for key in ("start_seconds",):
        if key in item and item[key] is not None and not _non_negative(item[key]):
            raise AgentYankersError(f"segments[{index}].{key} must be a non-negative number")
    return item


def _voice(payload: Any, index: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentYankersError(f"segments[{index}] voice must be an object")
    _exact_keys(
        payload,
        _VOICE_KEYS,
        required={"schema_version", "voice_id", "library", "provider"},
        label="voice",
    )
    _version(payload)
    voice = dict(payload)
    _match(_VOICE_ID, voice["voice_id"], "voice_id")
    if voice["library"] not in _LIBRARIES:
        raise AgentYankersError("voice library must be genaid-voices, dlm-voicemapper, or personaplex-prompt")
    if voice["provider"] not in _PROVIDERS:
        raise AgentYankersError("voice provider must be vibevoice or personaplex")
    relative = voice.get("relative_path")
    if relative is not None:
        voice["relative_path"] = _relative_voice_path(relative)
    return voice


def _relative_voice_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentYankersError("voice relative_path must be a relative path")
    if "\x00" in value or "\\" in value or value.startswith(("/", "\\")):
        raise AgentYankersError("voice paths must be relative to the voice library root")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AgentYankersError("voice paths must be relative to the voice library root")
    return "/".join(parts)


def _cue(payload: Any, index: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentYankersError(f"cues[{index}] must be an object")
    _exact_keys(
        payload,
        _CUE_KEYS,
        required={"schema_version", "cue_id", "kind", "placement", "anchor_id"},
        label=f"cues[{index}]",
    )
    _version(payload)
    cue = dict(payload)
    _match(_CUE_ID, cue["cue_id"], "cue_id")
    if cue["kind"] not in _CUE_KINDS:
        raise AgentYankersError(f"cues[{index}] kind is not a show cue")
    if cue["placement"] not in _PLACEMENTS:
        raise AgentYankersError(f"cues[{index}] placement is invalid")
    cue["anchor_id"] = _text(cue["anchor_id"], "anchor_id", 80)
    if "gain_db" in cue and cue["gain_db"] is not None:
        gain = cue["gain_db"]
        if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not -60.0 <= float(gain) <= 0.0:
            raise AgentYankersError("cue gain_db must be between -60 and 0")
        cue["gain_db"] = float(gain)
    if "duration_seconds" in cue and cue["duration_seconds"] is not None:
        _duration(cue["duration_seconds"], f"cues[{index}].duration_seconds", required=False)
    asset = cue.get("asset_ref")
    if asset is not None and (not isinstance(asset, str) or len(asset) > 240):
        raise AgentYankersError("cue asset_ref must be a string")
    return cue


def _chapter(payload: Any, index: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentYankersError(f"chapters[{index}] must be an object")
    _exact_keys(payload, _CHAPTER_KEYS, required=_CHAPTER_KEYS, label=f"chapters[{index}]")
    _version(payload)
    chapter = dict(payload)
    _match(_CHAPTER_ID, chapter["chapter_id"], "chapter_id")
    chapter["title"] = _text(chapter["title"], "title", 120)
    start = chapter["start_seconds"]
    end = chapter["end_seconds"]
    if not _non_negative(start) or not _positive(end) or float(end) <= float(start):
        raise AgentYankersError("chapter end_seconds must be after start_seconds")
    chapter["start_seconds"] = float(start)
    chapter["end_seconds"] = float(end)
    return chapter


def _consent(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentYankersError("consent must be an object")
    _exact_keys(
        payload,
        _CONSENT_KEYS,
        required=_CONSENT_KEYS - {"consented_at"},
        label="consent",
    )
    _version(payload)
    consent = dict(payload)
    if not isinstance(consent["opted_in"], bool) or not isinstance(consent["disclosure_accepted"], bool):
        raise AgentYankersError("consent flags must be booleans")
    _match(_CONSENT_ID, consent["consent_id"], "consent_id")
    if consent["scope"] not in _SCOPES:
        raise AgentYankersError("consent scope is invalid")
    stamped = consent.get("consented_at")
    if stamped is not None and (not isinstance(stamped, str) or not _ISO_TIME.fullmatch(stamped)):
        raise AgentYankersError("consented_at must be UTC ISO-8601, YYYY-MM-DDTHH:MM:SSZ")
    return consent


def _tags(values: list[Any]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise AgentYankersError("invalid tag")
        item = value.strip().lower()
        if not _TAG.fullmatch(item):
            raise AgentYankersError("invalid tag")
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


def _version(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise AgentYankersError("schema_version must be agentyankers/v1")


def _exact_keys(payload: dict[str, Any], allowed: set[str], *, required: set[str], label: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        names = ", ".join(sorted(str(item) for item in unknown))
        raise AgentYankersError(f"unknown {label} fields: {names}")
    missing = [key for key in sorted(required) if key not in payload]
    if missing:
        raise AgentYankersError(f"{label} is missing {', '.join(missing)}")


def _match(pattern: re.Pattern[str], value: Any, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AgentYankersError(f"invalid {label}")
    return value


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AgentYankersError(f"invalid {label}")
    return value


def _duration(value: Any, label: str, *, required: bool) -> float:
    if value is None and not required:
        raise AgentYankersError(f"invalid {label}")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) <= 30:
        raise AgentYankersError(f"{label} must be a duration above 0 and at most 30 seconds")
    return float(value)


def _non_negative(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and float(value) >= 0


def _positive(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and float(value) > 0
