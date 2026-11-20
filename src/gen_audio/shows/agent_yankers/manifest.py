"""Episode and highlight-reel manifests for genaid.

genaid does not import this package. It reads the JSON: walk ``segments`` in
order, concatenate a segment only when ``status`` is ``rendered``, and skip
``planned`` segments instead of synthesizing a fill. Call and highlight
segments are emitted only for marks that passed the consent check.
"""

from __future__ import annotations

from pathlib import Path

from gen_audio.shows.agent_yankers.errors import AgentYankersError, ConsentRefusal
from gen_audio.shows.agent_yankers.models import (
    Highlight,
    Mark,
    MarksIndex,
    Premise,
    VoiceoverPlan,
    consent_gaps,
)
from gen_audio.shows.agent_yankers.schema import load_schema, read_json, validate_instance

EPISODE_CONTRACT = "https://darbotlabs.dev/gen-audio/shows/agent_yankers/episode.v1.schema.json"
CONSUMER_RULES = (
    "Walk segments from first to last. Do not reorder them.",
    "Concatenate a segment only when status is rendered and asset is a relative wav path beside this manifest.",
    "Skip planned segments. Do not synthesize audio to fill them.",
    "A call or highlight is included only after the mark's recorded two-party consent passed the runner.",
    "This manifest holds no credentials. Do not read LIVEKIT_* or PERSONAPLEX_HF_TOKEN or GEN_AUDIO_VIBEVOICE_MODEL from it.",
)

_SOURCES = {
    "host": "vibevoice",
    "laughter": "sfx",
    "stinger": "sfx",
    "call": "livekit",
    "highlight": "livekit",
}
_FORBIDDEN = {
    "host": (
        "sfx_id",
        "mark_id",
        "is_agent",
        "consent_record_ref",
        "premise_id",
        "call_segment_id",
        "in_s",
        "out_s",
        "note",
    ),
    "laughter": (
        "text",
        "voice_stem",
        "mark_id",
        "is_agent",
        "consent_record_ref",
        "premise_id",
        "call_segment_id",
        "in_s",
        "out_s",
        "note",
    ),
    "stinger": (
        "text",
        "voice_stem",
        "mark_id",
        "is_agent",
        "consent_record_ref",
        "premise_id",
        "call_segment_id",
        "in_s",
        "out_s",
        "note",
    ),
    "call": ("text", "voice_stem", "sfx_id", "call_segment_id", "in_s", "out_s", "note"),
    "highlight": ("text", "voice_stem", "sfx_id"),
}


def load_episode(
    path: Path,
    *,
    marks: MarksIndex | None = None,
    records_root: Path | None = None,
) -> dict:
    document = read_json(path)
    check_episode(document, marks=marks, records_root=records_root)
    return document


def assemble_episode(
    *,
    episode_id: str,
    title: str,
    premise: Premise,
    marks: MarksIndex,
    voiceover: VoiceoverPlan,
    highlights: tuple[Highlight, ...] | list[Highlight],
    records_root: Path,
) -> dict:
    """Build a planned episode. Assets stay null. Refused marks fail the whole episode."""
    chosen = tuple(highlights)
    _highlights_cover_targets(premise, marks, chosen, records_root)
    segments: list[dict] = []
    for cue in voiceover.cues:
        if cue.placement == "open":
            segments.append(_cue_segment(cue, voiceover))
    highlight_rows: list[dict] = []
    for mark_id in premise.targets:
        mark = marks.get(mark_id)
        segments.append(_call_segment(premise, mark))
        for item in chosen:
            if item.mark_id != mark_id:
                continue
            segments.append(_highlight_segment(premise, mark, item))
            highlight_rows.append(_highlight_row(mark, item))
        for cue in voiceover.cues:
            if cue.placement == "after_call":
                segments.append(_cue_segment(cue, voiceover, suffix=mark_id))
    for cue in voiceover.cues:
        if cue.placement == "close":
            segments.append(_cue_segment(cue, voiceover))
    document = _document(
        episode_id=episode_id,
        title=title,
        kind="episode",
        premise_id=premise.id,
        highlights=highlight_rows,
        segments=segments,
    )
    check_episode(document, marks=marks, records_root=records_root)
    return document


def highlight_reel(episode: dict) -> dict:
    """The same episode with call segments removed. genaid still walks ``segments`` in order."""
    check_episode(episode)
    if episode["kind"] != "episode":
        raise AgentYankersError("a highlight reel is built from an episode", code="highlights")
    if not episode["highlights"]:
        raise AgentYankersError("a highlight reel needs at least one highlight", code="highlights")
    kept = [segment for segment in episode["segments"] if segment["role"] != "call"]
    if not any(segment["role"] == "highlight" for segment in kept):
        raise AgentYankersError("a highlight reel needs a highlight segment", code="highlights")
    reel_id = episode["id"] + "-highlights"
    document = _document(
        episode_id=reel_id,
        title=episode["title"] + " (highlights)",
        kind="highlight_reel",
        premise_id=episode["premise_id"],
        highlights=list(episode["highlights"]),
        segments=kept,
    )
    check_episode(document)
    return document


def check_episode(
    document: dict,
    *,
    marks: MarksIndex | None = None,
    records_root: Path | None = None,
) -> None:
    """Schema plus the edit rules genaid relies on."""
    validate_instance(document, load_schema("episode"))
    if list(document["consumer"]["rules"]) != list(CONSUMER_RULES):
        raise AgentYankersError("episode consumer.rules are not the genaid contract", code="episode")
    segments = document["segments"]
    ids = [segment["id"] for segment in segments]
    if len(ids) != len(set(ids)):
        raise AgentYankersError("episode segment ids must be unique", code="episode")
    by_id = {segment["id"]: segment for segment in segments}
    for segment in segments:
        _check_segment(segment)
    _check_highlight_index(document, by_id)
    if marks is not None:
        _check_marks(document, marks, records_root)


def _document(
    *,
    episode_id: str,
    title: str,
    kind: str,
    premise_id: str,
    highlights: list[dict],
    segments: list[dict],
) -> dict:
    return {
        "schema_version": "1.0.0",
        "show": "agent_yankers",
        "kind": kind,
        "id": episode_id,
        "title": title,
        "premise_id": premise_id,
        "sample_rate": 24000,
        "channels": 1,
        "consumer": {
            "name": "genaid",
            "contract": EPISODE_CONTRACT,
            "order": "segments",
            "rules": list(CONSUMER_RULES),
        },
        "highlights": highlights,
        "segments": segments,
    }


def _cue_segment(cue, voiceover: VoiceoverPlan, suffix: str | None = None) -> dict:
    segment_id = cue.id if suffix is None else f"{cue.id}-{suffix}"
    segment: dict = {
        "id": segment_id,
        "role": cue.role,
        "status": "planned",
        "source": _SOURCES[cue.role],
        "asset": None,
    }
    if cue.role == "host":
        segment["text"] = cue.text
        segment["voice_stem"] = voiceover.host_voice.file_stem
    else:
        segment["sfx_id"] = cue.sfx_id
    return segment


def _call_segment(premise: Premise, mark: Mark) -> dict:
    return {
        "id": f"call-{mark.id}",
        "role": "call",
        "status": "planned",
        "source": "livekit",
        "asset": None,
        "mark_id": mark.id,
        "is_agent": mark.is_agent,
        "consent_record_ref": mark.consent.record_ref,
        "premise_id": premise.id,
    }


def _highlight_segment(premise: Premise, mark: Mark, item: Highlight) -> dict:
    return {
        "id": item.id,
        "role": "highlight",
        "status": "planned",
        "source": "livekit",
        "asset": None,
        "mark_id": mark.id,
        "is_agent": mark.is_agent,
        "consent_record_ref": mark.consent.record_ref,
        "premise_id": premise.id,
        "call_segment_id": f"call-{mark.id}",
        "in_s": item.in_s,
        "out_s": item.out_s,
        "note": item.note,
    }


def _highlight_row(mark: Mark, item: Highlight) -> dict:
    return {
        "id": item.id,
        "segment_id": item.id,
        "call_segment_id": f"call-{mark.id}",
        "mark_id": mark.id,
        "in_s": item.in_s,
        "out_s": item.out_s,
        "note": item.note,
    }


def _highlights_cover_targets(
    premise: Premise,
    marks: MarksIndex,
    highlights: tuple[Highlight, ...],
    records_root: Path,
) -> None:
    refusals: list[tuple[str, list[str]]] = []
    for mark_id in premise.targets:
        gaps = consent_gaps(marks.get(mark_id), records_root)
        if gaps:
            refusals.append((mark_id, gaps))
    if refusals:
        raise ConsentRefusal(refusals)
    target_set = set(premise.targets)
    for item in highlights:
        if item.mark_id not in target_set:
            raise AgentYankersError(
                f"highlight {item.id} names {item.mark_id}, which is not a premise target",
                code="highlights",
            )


def _check_segment(segment: dict) -> None:
    role = segment["role"]
    if segment["source"] != _SOURCES[role]:
        raise AgentYankersError(
            f"segment {segment['id']} source must be {_SOURCES[role]}",
            code="episode",
        )
    for key in _FORBIDDEN[role]:
        if key in segment:
            raise AgentYankersError(
                f"segment {segment['id']} role {role} must not include {key}",
                code="episode",
            )
    if role == "host" and ("text" not in segment or "voice_stem" not in segment):
        raise AgentYankersError(f"host segment {segment['id']} needs text and voice_stem", code="episode")
    if role in {"laughter", "stinger"} and "sfx_id" not in segment:
        raise AgentYankersError(f"segment {segment['id']} needs sfx_id", code="episode")
    if role in {"call", "highlight"}:
        for key in ("mark_id", "is_agent", "consent_record_ref", "premise_id"):
            if key not in segment:
                raise AgentYankersError(f"segment {segment['id']} needs {key}", code="episode")
        if segment["id"] == f"call-{segment['mark_id']}" and role != "call":
            raise AgentYankersError(f"segment {segment['id']} uses a call id", code="episode")
    if role == "call" and segment["id"] != f"call-{segment['mark_id']}":
        raise AgentYankersError(
            f"call segment {segment['id']} must be named call-{{mark_id}}",
            code="episode",
        )
    if role == "highlight":
        for key in ("call_segment_id", "in_s", "out_s", "note"):
            if key not in segment:
                raise AgentYankersError(f"highlight segment {segment['id']} needs {key}", code="episode")
        if segment["out_s"] <= segment["in_s"]:
            raise AgentYankersError(
                f"highlight segment {segment['id']} out_s must be after in_s",
                code="episode",
            )
        if segment["call_segment_id"] != f"call-{segment['mark_id']}":
            raise AgentYankersError(
                f"highlight segment {segment['id']} call_segment_id does not match its mark",
                code="episode",
            )
    status = segment["status"]
    asset = segment["asset"]
    if status == "planned" and asset is not None:
        raise AgentYankersError(
            f"planned segment {segment['id']} must not name an asset",
            code="episode",
        )
    if status == "rendered" and not isinstance(asset, str):
        raise AgentYankersError(
            f"rendered segment {segment['id']} needs a relative wav asset",
            code="episode",
        )


def _check_highlight_index(document: dict, by_id: dict[str, dict]) -> None:
    rows = document["highlights"]
    row_ids = [row["id"] for row in rows]
    if len(row_ids) != len(set(row_ids)):
        raise AgentYankersError("highlight ids must be unique", code="episode")
    segment_ids = [segment["id"] for segment in document["segments"] if segment["role"] == "highlight"]
    if sorted(row_ids) != sorted(segment_ids):
        raise AgentYankersError(
            "highlights[] and highlight segments must name the same ids",
            code="episode",
        )
    calls = {segment["id"] for segment in document["segments"] if segment["role"] == "call"}
    if document["kind"] == "highlight_reel" and calls:
        raise AgentYankersError("a highlight reel does not include call segments", code="episode")
    for row in rows:
        segment = by_id[row["segment_id"]]
        if segment["role"] != "highlight":
            raise AgentYankersError(f"highlight {row['id']} does not point at a highlight segment", code="episode")
        for key in ("call_segment_id", "mark_id", "in_s", "out_s", "note"):
            if segment[key] != row[key]:
                raise AgentYankersError(
                    f"highlight {row['id']} does not match its segment",
                    code="episode",
                )
        if document["kind"] == "episode" and row["call_segment_id"] not in calls:
            raise AgentYankersError(
                f"highlight {row['id']} has no call segment in this episode",
                code="episode",
            )
        if row["out_s"] <= row["in_s"]:
            raise AgentYankersError(f"highlight {row['id']} out_s must be after in_s", code="episode")


def _check_marks(document: dict, marks: MarksIndex, records_root: Path | None) -> None:
    refusals: list[tuple[str, list[str]]] = []
    for segment in document["segments"]:
        if segment["role"] not in {"call", "highlight"}:
            continue
        mark_id = segment["mark_id"]
        try:
            mark = marks.get(mark_id)
        except AgentYankersError:
            refusals.append((mark_id, [f"unknown mark {mark_id}"]))
            continue
        gaps = list(consent_gaps(mark, records_root))
        if segment["is_agent"] is not mark.is_agent:
            gaps.append("is_agent does not match the marks index")
        if segment["consent_record_ref"] != mark.consent.record_ref:
            gaps.append("consent_record_ref does not match the marks index")
        if segment["premise_id"] != document["premise_id"]:
            gaps.append("premise_id does not match the episode")
        if gaps:
            refusals.append((mark_id, gaps))
    if refusals:
        raise ConsentRefusal(refusals)
