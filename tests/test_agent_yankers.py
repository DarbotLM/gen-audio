"""Agent Yankers schemas, consent refusal, and the genaid episode manifest."""

from __future__ import annotations

import json
from dataclasses import replace

from gen_audio.cli.agent_yankers import main
from gen_audio.shows.agent_yankers import (
    CallRunner,
    ConsentRefusal,
    IntegrationUnavailable,
    StubVibeVoiceShowAudio,
    assemble_episode,
    build_session,
    check_episode,
    highlight_reel,
    load_episode,
    load_highlights,
    load_marks,
    load_premise,
    load_voiceover,
)
from gen_audio.shows.agent_yankers.errors import AgentYankersError, SchemaError
from gen_audio.shows.agent_yankers.models import MarksIndex, consent_gaps
from gen_audio.shows.agent_yankers.schema import SCHEMA_FILES, dump_json, example_dir, load_schema, validate_instance
from gen_audio.shows.agent_yankers.session import StubLiveKitTransport, TransportProbe

ROOT = example_dir()
SECRET = "super-secret-token-xyz"


def _load_parts():
    return (
        load_premise(ROOT / "premise.example.json"),
        load_marks(ROOT / "marks.example.json"),
        load_voiceover(ROOT / "voiceover.example.json"),
        load_highlights(ROOT / "highlights.example.json"),
    )


def _planned():
    premise, marks, voiceover, highlights = _load_parts()
    return assemble_episode(
        episode_id="ay-001",
        title="The ticket that was never filed",
        premise=premise,
        marks=marks,
        voiceover=voiceover,
        highlights=highlights,
        records_root=ROOT,
    )


class _Spy:
    def __init__(self, runnable: bool):
        self.runnable = runnable
        self.probes = 0
        self.opened: list[dict] = []

    def probe(self) -> TransportProbe:
        self.probes += 1
        missing = () if self.runnable else ("livekit agents SDK is not wired in this package",)
        return TransportProbe("livekit", self.runnable, missing, True)

    def open_session(self, session: dict) -> None:
        self.opened.append(session)


def test_shipped_schemas_load_and_examples_match_them():
    for name in SCHEMA_FILES:
        schema = load_schema(name)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    premise, marks, voiceover, highlights = _load_parts()
    assert premise.id == "premise-hold-music"
    assert [mark.id for mark in marks.marks] == ["mark-desk-agent", "mark-switchboard-agent"]
    assert all(mark.is_agent is True and "Fictional" in mark.notes for mark in marks.marks)
    assert voiceover.host_voice.file_stem == "en-Alice_woman"
    assert len(highlights) == 2
    episode = json.loads((ROOT / "episode.example.json").read_text(encoding="utf-8"))
    reel = json.loads((ROOT / "episode.highlights.example.json").read_text(encoding="utf-8"))
    session = json.loads((ROOT / "session.desk-agent.example.json").read_text(encoding="utf-8"))
    validate_instance(episode, load_schema("episode"))
    validate_instance(reel, load_schema("episode"))
    validate_instance(session, load_schema("session"))


def test_examples_are_the_assembler_output():
    episode = _planned()
    assert dump_json(episode) == (ROOT / "episode.example.json").read_text(encoding="utf-8")
    assert dump_json(highlight_reel(episode)) == (ROOT / "episode.highlights.example.json").read_text(encoding="utf-8")
    premise, marks, _, _ = _load_parts()
    session = build_session(premise, marks.get("mark-desk-agent"), ROOT)
    assert dump_json(session) == (ROOT / "session.desk-agent.example.json").read_text(encoding="utf-8")


def test_schema_rejects_unknown_fields_and_a_bad_date():
    premise = json.loads((ROOT / "premise.example.json").read_text(encoding="utf-8"))
    premise["script"] = "Speaker 1: read this."
    try:
        validate_instance(premise, load_schema("premise"))
    except SchemaError as exc:
        assert exc.code == "schema"
        assert "script" in str(exc)
    else:
        raise AssertionError("unknown field was accepted")

    marks = json.loads((ROOT / "marks.example.json").read_text(encoding="utf-8"))
    marks["marks"][0]["consent"]["recorded_at"] = "2026-02-31T15:00:00Z"
    try:
        validate_instance(marks, load_schema("marks"))
    except SchemaError as exc:
        assert "recorded_at" in str(exc)
    else:
        raise AssertionError("impossible date was accepted")


def test_premise_rejects_a_script_line_and_a_missing_consent_boundary(tmp_path):
    raw = json.loads((ROOT / "premise.example.json").read_text(encoding="utf-8"))
    raw["outline"][0] = "Speaker 1: read this line aloud."
    path = tmp_path / "premise.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    try:
        load_premise(path)
    except AgentYankersError as exc:
        assert exc.code == "premise"
    else:
        raise AssertionError("script line was accepted as an outline")

    raw = json.loads((ROOT / "premise.example.json").read_text(encoding="utf-8"))
    raw["boundaries"] = ["Be brief and stay on the outline."]
    path.write_text(json.dumps(raw), encoding="utf-8")
    try:
        load_premise(path)
    except AgentYankersError as exc:
        assert exc.code == "boundaries"
    else:
        raise AssertionError("boundaries without consent were accepted")


def test_runner_refuses_a_mark_without_recorded_consent_before_the_transport():
    premise, marks, _, _ = _load_parts()
    desk = marks.get("mark-desk-agent")
    refused = replace(desk, consent=replace(desk.consent, opt_in=False, recorded=False))
    index = MarksIndex((refused, marks.get("mark-switchboard-agent")))
    spy = _Spy(runnable=True)
    runner = CallRunner(ROOT, spy)
    try:
        runner.place(premise, index)
    except ConsentRefusal as exc:
        assert exc.code == "consent_refused"
        assert "No call was placed" in str(exc)
        assert any(mark_id == "mark-desk-agent" for mark_id, _reasons in exc.refusals)
        assert "opt_in is not true" in exc.refusals[0][1]
    else:
        raise AssertionError("a mark without opt-in was called")
    assert spy.probes == 0
    assert spy.opened == []


def test_consent_gaps_cover_two_party_record_and_the_file(tmp_path):
    mark = load_marks(ROOT / "marks.example.json").get("mark-desk-agent")
    assert consent_gaps(mark, ROOT) == []

    one_party = replace(mark, consent=replace(mark.consent, parties=("caller", "caller")))
    assert "two-party consent requires both caller and mark" in consent_gaps(one_party, ROOT)

    missing_file = consent_gaps(mark, tmp_path)
    assert "consent record file is not on disk" in missing_file

    (tmp_path / "consent").mkdir()
    (tmp_path / "consent" / "desk-agent.txt").write_text(
        "mark_id: mark-desk-agent\nNo permission is written in this file.\n",
        encoding="utf-8",
    )
    assert "consent record does not state an opt-in" in consent_gaps(mark, tmp_path)

    escaped = replace(mark, consent=replace(mark.consent, record_ref="consent/../desk-agent.txt"))
    assert "consent record_ref escapes the records directory" in consent_gaps(escaped, ROOT)


def test_is_agent_false_still_requires_consent_and_can_pass_when_it_is_recorded(tmp_path):
    premise, marks, _, _ = _load_parts()
    human = replace(
        marks.get("mark-desk-agent"),
        is_agent=False,
        notes="A person who recorded written consent for this fictional test.",
    )
    bare = replace(human, consent=replace(human.consent, opt_in=False, recorded=False, record_ref=None))
    try:
        build_session(premise, bare, ROOT)
    except ConsentRefusal as exc:
        assert "opt_in is not true" in str(exc)
    else:
        raise AssertionError("is_agent false skipped the consent check")

    (tmp_path / "consent").mkdir()
    (tmp_path / "consent" / "desk-agent.txt").write_text(
        "mark_id: mark-desk-agent\nThis person opts in to being called and recorded.\n",
        encoding="utf-8",
    )
    only_desk = replace(premise, targets=("mark-desk-agent",))
    session = build_session(only_desk, human, tmp_path)
    assert session["is_agent"] is False
    assert session["recording"]["requires_recorded_consent"] is True


def test_place_stops_at_the_stub_and_does_not_leak_credentials(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", SECRET)
    monkeypatch.setenv("LIVEKIT_API_SECRET", SECRET)
    monkeypatch.setenv("PERSONAPLEX_HF_TOKEN", SECRET)
    premise, marks, _, _ = _load_parts()
    runner = CallRunner(ROOT, StubLiveKitTransport())
    try:
        runner.place(premise, marks)
    except IntegrationUnavailable as exc:
        assert exc.code == "unavailable"
        assert "No audio was written" in str(exc)
        assert "livekit agents SDK is not wired" in str(exc)
        assert SECRET not in str(exc)
    else:
        raise AssertionError("stub transport placed a call")
    probe = StubLiveKitTransport().probe()
    assert probe.runnable is False
    assert probe.credentials_present is True
    blob = json.dumps(probe.as_dict())
    assert SECRET not in blob
    session = build_session(premise, marks.get("mark-desk-agent"), ROOT)
    assert SECRET not in dump_json(session)


def test_runnable_transport_opens_only_after_consent():
    premise, marks, _, _ = _load_parts()
    spy = _Spy(runnable=True)
    CallRunner(ROOT, spy).place(premise, marks)
    assert spy.probes == 1
    assert [item["mark_id"] for item in spy.opened] == ["mark-desk-agent", "mark-switchboard-agent"]


def test_episode_manifest_rules_and_genaid_consumer():
    episode = load_episode(ROOT / "episode.example.json", marks=load_marks(ROOT / "marks.example.json"), records_root=ROOT)
    assert episode["consumer"]["name"] == "genaid"
    assert episode["consumer"]["order"] == "segments"
    assert episode["sample_rate"] == 24000
    roles = [segment["role"] for segment in episode["segments"]]
    assert roles[0] == "stinger"
    assert "call" in roles and "highlight" in roles and "host" in roles and "laughter" in roles
    assert all(segment["status"] == "planned" and segment["asset"] is None for segment in episode["segments"])
    calls = [segment for segment in episode["segments"] if segment["role"] == "call"]
    assert all(segment["is_agent"] is True and segment["consent_record_ref"].startswith("consent/") for segment in calls)
    reel = highlight_reel(episode)
    assert reel["kind"] == "highlight_reel"
    assert all(segment["role"] != "call" for segment in reel["segments"])
    assert [row["id"] for row in reel["highlights"]] == ["hl-desk-ticket", "hl-switch-hold"]


def test_manifest_rejects_a_planned_asset_and_a_non_consenting_call():
    episode = _planned()
    episode["segments"][1]["asset"] = "audio/host-open.wav"
    try:
        check_episode(episode)
    except AgentYankersError as exc:
        assert exc.code == "episode"
        assert "planned" in str(exc)
    else:
        raise AssertionError("planned segment was allowed to name an asset")

    episode = _planned()
    marks = load_marks(ROOT / "marks.example.json")
    desk = replace(marks.get("mark-desk-agent"), consent=replace(marks.get("mark-desk-agent").consent, recorded=False))
    try:
        check_episode(episode, marks=MarksIndex((desk, marks.get("mark-switchboard-agent"))), records_root=ROOT)
    except ConsentRefusal as exc:
        assert "mark-desk-agent" in str(exc)
    else:
        raise AssertionError("episode kept a mark whose consent is not recorded")


def test_manifest_rejects_an_escaping_asset_path():
    episode = _planned()
    episode["segments"][0]["status"] = "rendered"
    episode["segments"][0]["asset"] = "../secret.wav"
    try:
        check_episode(episode)
    except SchemaError as exc:
        assert "asset" in str(exc)
    else:
        raise AssertionError("parent-path asset was accepted")


def test_unknown_mark_and_off_target_highlight_do_not_build_an_episode():
    premise, marks, voiceover, highlights = _load_parts()
    shared = {
        "episode_id": "ay-001",
        "title": "The ticket that was never filed",
        "premise": premise,
        "voiceover": voiceover,
        "records_root": ROOT,
    }
    try:
        assemble_episode(marks=MarksIndex(()), highlights=highlights, **shared)
    except AgentYankersError as exc:
        assert exc.code == "unknown_mark"
    else:
        raise AssertionError("missing mark was assembled")
    off = replace(highlights[0], mark_id="mark-not-in-premise")
    try:
        assemble_episode(marks=marks, highlights=(off, highlights[1]), **shared)
    except AgentYankersError as exc:
        assert exc.code == "highlights"
    else:
        raise AssertionError("highlight for a non-target was assembled")


def test_assemble_refuses_the_episode_when_any_target_lacks_consent():
    premise, marks, voiceover, highlights = _load_parts()
    bad = replace(
        marks.get("mark-switchboard-agent"),
        consent=replace(marks.get("mark-switchboard-agent").consent, opt_in=False),
    )
    try:
        assemble_episode(
            episode_id="ay-001",
            title="The ticket that was never filed",
            premise=premise,
            marks=MarksIndex((marks.get("mark-desk-agent"), bad)),
            voiceover=voiceover,
            highlights=highlights,
            records_root=ROOT,
        )
    except ConsentRefusal as exc:
        assert "mark-switchboard-agent" in str(exc)
        assert "No call was placed" in str(exc)
    else:
        raise AssertionError("episode included a mark that did not opt in")


def test_voiceover_stub_copies_sfx_and_refuses_host_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("GEN_AUDIO_AGENT_YANKERS_SFX_DIR", str(tmp_path))
    monkeypatch.setenv("GEN_AUDIO_VIBEVOICE_MODEL", SECRET)
    bed = tmp_path / "crowd-laugh-short.wav"
    bed.write_bytes(b"RIFFstub-laugh")
    _premise, _marks, voiceover, _highlights = _load_parts()
    laugh = next(cue for cue in voiceover.cues if cue.role == "laughter")
    host = next(cue for cue in voiceover.cues if cue.role == "host")
    output = tmp_path / "out" / "laugh.wav"
    output.parent.mkdir()
    result = StubVibeVoiceShowAudio().render(laugh, output)
    assert result.synthesized_speech is False
    assert result.source == "sfx"
    assert output.read_bytes() == b"RIFFstub-laugh"
    host_out = tmp_path / "out" / "host.wav"
    try:
        StubVibeVoiceShowAudio().render(host, host_out)
    except IntegrationUnavailable as exc:
        assert "No audio was written" in str(exc)
        assert SECRET not in str(exc)
        assert "not wired to a VibeVoice render" in str(exc)
    else:
        raise AssertionError("host line was synthesized")
    assert not host_out.exists()
    probe = StubVibeVoiceShowAudio().probe()
    assert probe.runnable is False
    assert SECRET not in json.dumps(probe.as_dict())


def test_cli_validates_and_plans_the_example(capsys):
    assert main(["validate-marks", str(ROOT / "marks.example.json")]) == 0
    assert "ok marks 2" in capsys.readouterr().out
    assert main(["validate-episode", str(ROOT / "episode.example.json"), "--marks", str(ROOT / "marks.example.json"), "--records", str(ROOT)]) == 0
    capsys.readouterr()
    code = main(
        [
            "plan",
            "--premise",
            str(ROOT / "premise.example.json"),
            "--marks",
            str(ROOT / "marks.example.json"),
            "--voiceover",
            str(ROOT / "voiceover.example.json"),
            "--highlights",
            str(ROOT / "highlights.example.json"),
            "--records",
            str(ROOT),
            "--episode-id",
            "ay-001",
            "--title",
            "The ticket that was never filed",
        ]
    )
    assert code == 0
    assert capsys.readouterr().out == (ROOT / "episode.example.json").read_text(encoding="utf-8")
    assert main(["probe"]) == 0
    probe = json.loads(capsys.readouterr().out)
    assert probe["livekit"]["runnable"] is False
    assert probe["vibevoice"]["runnable"] is False
    assert probe["livekit"]["audio_written"] is False


def test_cli_returns_one_when_consent_fails(tmp_path, capsys):
    raw = json.loads((ROOT / "marks.example.json").read_text(encoding="utf-8"))
    raw["marks"][0]["consent"]["opt_in"] = False
    raw["marks"][0]["consent"]["recorded"] = False
    path = tmp_path / "marks.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    code = main(
        [
            "plan",
            "--premise",
            str(ROOT / "premise.example.json"),
            "--marks",
            str(path),
            "--voiceover",
            str(ROOT / "voiceover.example.json"),
            "--highlights",
            str(ROOT / "highlights.example.json"),
            "--records",
            str(ROOT),
            "--episode-id",
            "ay-001",
            "--title",
            "The ticket that was never filed",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "consent_refused" in err
    assert "No call was placed" in err
