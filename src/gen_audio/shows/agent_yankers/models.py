"""Premise, marks, voiceover, and highlight documents.

Schema files check the shape. The functions here add the cross-field rules a
schema cannot express: an outline is not a script, the host voice stem matches
its prefix, and a mark's consent record is actually on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from gen_audio.shows.agent_yankers.errors import AgentYankersError
from gen_audio.shows.agent_yankers.schema import load_schema, read_json, validate_instance

_VOICE_CODE = re.compile(r"^(NATF[0-3]|NATM[0-3]|VARF[0-4]|VARM[0-4])\Z")
_STEM = re.compile(r"^[a-z]{2,3}-[A-Za-z0-9]+_(man|woman|neutral)(_bgm)?\Z")
_MAX_CONSENT_BYTES = 64 * 1024


@dataclass(frozen=True)
class Caller:
    agent_name: str
    persona_prompt: str
    voice_code: str

    @property
    def voice_prompt(self) -> str:
        return f"{self.voice_code}.pt"

    @property
    def identity(self) -> str:
        return _slug(self.agent_name)


@dataclass(frozen=True)
class Premise:
    id: str
    title: str
    outline: tuple[str, ...]
    caller: Caller
    targets: tuple[str, ...]
    boundaries: tuple[str, ...]


@dataclass(frozen=True)
class Consent:
    opt_in: bool
    recorded: bool
    recorded_at: str | None
    method: str | None
    record_ref: str | None
    parties: tuple[str, ...]
    statement: str


@dataclass(frozen=True)
class Mark:
    id: str
    display_name: str
    is_agent: bool
    identity: str
    consent: Consent
    notes: str


@dataclass(frozen=True)
class MarksIndex:
    marks: tuple[Mark, ...]

    def get(self, mark_id: str) -> Mark:
        for mark in self.marks:
            if mark.id == mark_id:
                return mark
        raise AgentYankersError(f"unknown mark {mark_id}", code="unknown_mark")


@dataclass(frozen=True)
class HostVoice:
    file_stem: str
    file_prefix: str
    background_music: bool


@dataclass(frozen=True)
class Cue:
    id: str
    role: str
    placement: str
    text: str | None = None
    sfx_id: str | None = None


@dataclass(frozen=True)
class VoiceoverPlan:
    host_voice: HostVoice
    cues: tuple[Cue, ...]
    model_env: str
    sfx_dir_env: str


@dataclass(frozen=True)
class Highlight:
    id: str
    mark_id: str
    in_s: float
    out_s: float
    note: str


def load_premise(path: Path) -> Premise:
    document = _validated(path, "premise")
    outline = tuple(document["outline"])
    for index, line in enumerate(outline):
        if line.lower().startswith("speaker "):
            raise AgentYankersError(
                f"outline item {index + 1} is a script line; a premise is an outline the agent improvises from",
                code="premise",
            )
    boundaries = tuple(document["boundaries"])
    if "consent" not in " ".join(boundaries).lower():
        raise AgentYankersError(
            "premise boundaries must say what happens when consent is withdrawn",
            code="boundaries",
        )
    caller = document["caller"]
    prompt = caller["persona_prompt"]
    if "improvis" not in prompt.lower():
        raise AgentYankersError(
            "caller persona_prompt must tell the agent to improvise",
            code="premise",
        )
    if not _VOICE_CODE.fullmatch(caller["voice_code"]):
        raise AgentYankersError("caller voice_code is not a PersonaPlex stock code", code="premise")
    targets = tuple(document["targets"])
    if len(targets) != len(set(targets)):
        raise AgentYankersError("premise targets must be unique", code="premise")
    return Premise(
        id=document["id"],
        title=document["title"],
        outline=outline,
        caller=Caller(
            agent_name=caller["agent_name"],
            persona_prompt=prompt,
            voice_code=caller["voice_code"],
        ),
        targets=targets,
        boundaries=boundaries,
    )


def load_marks(path: Path) -> MarksIndex:
    document = _validated(path, "marks")
    marks = tuple(_mark(row) for row in document["marks"])
    ids = [mark.id for mark in marks]
    if len(ids) != len(set(ids)):
        raise AgentYankersError("mark ids must be unique", code="marks")
    return MarksIndex(marks)


def load_voiceover(path: Path) -> VoiceoverPlan:
    document = _validated(path, "voiceover")
    host = document["host_voice"]
    voice = HostVoice(
        file_stem=host["file_stem"],
        file_prefix=host["file_prefix"],
        background_music=host["background_music"],
    )
    _check_host_voice(voice)
    cues = tuple(_cue(row) for row in document["cues"])
    ids = [cue.id for cue in cues]
    if len(ids) != len(set(ids)):
        raise AgentYankersError("voiceover cue ids must be unique", code="voiceover")
    return VoiceoverPlan(
        host_voice=voice,
        cues=cues,
        model_env=document["model_env"],
        sfx_dir_env=document["sfx_dir_env"],
    )


def load_highlights(path: Path) -> tuple[Highlight, ...]:
    document = _validated(path, "highlights")
    found = tuple(_highlight(row) for row in document["highlights"])
    ids = [item.id for item in found]
    if len(ids) != len(set(ids)):
        raise AgentYankersError("highlight ids must be unique", code="highlights")
    return found


def consent_gaps(mark: Mark, records_root: Path | None = None) -> list[str]:
    """Reasons this mark cannot be called. Empty means recorded two-party consent is on file.

    ``is_agent`` is not a gap and it is not a waiver. A human mark and an agent
    mark both need the same recorded consent. ``records_root`` checks that the
    consent file named by ``record_ref`` exists under that directory.
    """
    gaps: list[str] = []
    consent = mark.consent
    if consent.opt_in is not True:
        gaps.append("opt_in is not true")
    if consent.recorded is not True:
        gaps.append("consent is not recorded")
    if not consent.recorded_at:
        gaps.append("recorded_at is missing")
    if consent.method not in {"written", "recorded_verbal"}:
        gaps.append("consent method is missing")
    if not consent.record_ref:
        gaps.append("consent record_ref is missing")
    parties = set(consent.parties)
    if parties != {"caller", "mark"}:
        gaps.append("two-party consent requires both caller and mark")
    if not consent.statement.strip():
        gaps.append("consent statement is empty")
    if records_root is not None and consent.record_ref and not any(
        reason.startswith("consent record_ref") for reason in gaps
    ):
        gaps.extend(_record_gaps(mark, records_root))
    return gaps


def _validated(path: Path, schema_name: str) -> dict:
    document = read_json(path)
    validate_instance(document, load_schema(schema_name))
    if not isinstance(document, dict):
        raise AgentYankersError(f"{path} must be a JSON object", code="schema")
    return document


def _mark(row: dict) -> Mark:
    consent = row["consent"]
    return Mark(
        id=row["id"],
        display_name=row["display_name"],
        is_agent=row["is_agent"],
        identity=row["endpoint"]["identity"],
        notes=row["notes"],
        consent=Consent(
            opt_in=consent["opt_in"],
            recorded=consent["recorded"],
            recorded_at=consent["recorded_at"],
            method=consent["method"],
            record_ref=consent["record_ref"],
            parties=tuple(consent["parties"]),
            statement=consent["statement"],
        ),
    )


def _cue(row: dict) -> Cue:
    role = row["role"]
    text = row.get("text")
    sfx_id = row.get("sfx_id")
    if role == "host":
        if not text or sfx_id is not None:
            raise AgentYankersError(f"host cue {row['id']} needs text and no sfx_id", code="voiceover")
    elif role in {"laughter", "stinger"}:
        if text is not None or not sfx_id:
            raise AgentYankersError(
                f"{role} cue {row['id']} needs sfx_id and no text",
                code="voiceover",
            )
    else:
        raise AgentYankersError(f"cue {row['id']} has role {role}", code="voiceover")
    return Cue(id=row["id"], role=role, placement=row["placement"], text=text, sfx_id=sfx_id)


def _highlight(row: dict) -> Highlight:
    if row["out_s"] <= row["in_s"]:
        raise AgentYankersError(
            f"highlight {row['id']} out_s must be after in_s",
            code="highlights",
        )
    return Highlight(
        id=row["id"],
        mark_id=row["mark_id"],
        in_s=float(row["in_s"]),
        out_s=float(row["out_s"]),
        note=row["note"],
    )


def _check_host_voice(voice: HostVoice) -> None:
    if _STEM.fullmatch(voice.file_stem) is None:
        raise AgentYankersError("host voice file_stem is not a VibeVoice stem", code="voice")
    prefix = voice.file_stem.split("-", 1)[0]
    if prefix != voice.file_prefix:
        raise AgentYankersError(
            f"host voice file_prefix {voice.file_prefix} does not match {voice.file_stem}",
            code="voice",
        )
    if voice.file_stem.endswith("_bgm") != voice.background_music:
        raise AgentYankersError(
            "host voice background_music must follow the _bgm suffix",
            code="voice",
        )


def _record_gaps(mark: Mark, records_root: Path) -> list[str]:
    ref = mark.consent.record_ref or ""
    parts = ref.split("/")
    if not records_root.is_dir():
        return ["consent records directory is not a directory"]
    if any(part in {"", ".", ".."} for part in parts) or "\\" in ref or ref.startswith("/"):
        return ["consent record_ref escapes the records directory"]
    root = records_root.resolve()
    candidate = (root / ref).resolve()
    if not candidate.is_relative_to(root):
        return ["consent record_ref escapes the records directory"]
    if not candidate.is_file():
        return ["consent record file is not on disk"]
    if candidate.stat().st_size > _MAX_CONSENT_BYTES:
        return ["consent record is larger than 64 KiB"]
    try:
        text = candidate.read_text(encoding="utf-8")
    except UnicodeError:
        return ["consent record is not utf-8"]
    gaps: list[str] = []
    if mark.id not in text:
        gaps.append("consent record does not name this mark")
    lowered = text.lower()
    if "opt in" not in lowered and "opts in" not in lowered:
        gaps.append("consent record does not state an opt-in")
    return gaps


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "agent"
