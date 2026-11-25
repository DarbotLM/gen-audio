"""AgentYankers format: consent, dry-run export, and the serve route."""

from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.request import Request, urlopen

import numpy as np
import pytest

from gen_audio.adapters import EngineRefusal, Probe
from gen_audio.agentyankers import (
    ENV_VIBEVOICE_MODEL,
    SCHEMA_VERSION,
    GenAudioVoiceoverRenderer,
    assemble_with_core,
    commentary_script,
    core_status,
    export_json_schemas,
    recording_disclosure,
    render_episode,
    require_call_consent,
    require_publish_consent,
)
from gen_audio.agentyankers.contract import ConsentRefusal, PhoneNumberRefusal
from gen_audio.agentyankers.core import reset_core_cache
from gen_audio.agentyankers.serve_route import handle_episode
from gen_audio.agentyankers.voiceover import render_multispeaker
from gen_audio.audio_io import read_wav, write_wav
from gen_audio.engines import get_engine
from gen_audio.node_http import dispatch, serve_loopback
from gen_audio.serve import agentyankers_episode_url

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "agentyankers" / "dry_run.json"
DISCLOSURE = recording_disclosure()


def _body() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _tone(path: Path) -> None:
    count = 24_000 // 10
    time = np.arange(count) / 24_000
    audio = (0.2 * np.sin(2 * np.pi * 330 * time)).astype(np.float32)
    write_wav(path, audio, 24_000)


def test_fixture_dry_run_writes_the_episode_bundle(tmp_path: Path):
    payload = _body()
    rendered = render_episode(
        payload["rundown"],
        payload["marks"],
        mode="dry_run",
        output_dir=tmp_path,
        seed=payload["seed"],
        logline=payload["logline"],
    )
    names = {
        "episode.wav",
        "chapters.json",
        "show_notes.md",
        "show_notes.json",
        "quality.json",
        "rundown.json",
        "spectrogram.png",
        "cube.png",
    }
    assert names <= {path.name for path in tmp_path.iterdir()}
    audio, rate = read_wav(tmp_path / "episode.wav")
    assert rate == 24_000
    assert audio.size / rate >= 0.5
    notes = (tmp_path / "show_notes.md").read_text(encoding="utf-8")
    packed = (tmp_path / "show_notes.json").read_text(encoding="utf-8")
    assert DISCLOSURE in notes
    assert "agent:desk-moss" not in notes
    assert "agent:desk-moss" not in packed
    assert "lk-identity:lamp-union" not in packed
    quality = json.loads((tmp_path / "quality.json").read_text(encoding="utf-8"))
    assert quality["schema"] == "agentyankers/audio-quality/v1"
    assert quality["speech_synthesized"] is False
    assert quality["voiceover"] == "placeholder"
    assert quality["gate"]["passed"] is True
    assert quality["spectrogram"]["frame_count"] > 0
    assert quality["cube"]["before"]["n"] > 0
    assert quality["cube"]["after"]["n"] == quality["cube"]["before"]["n"]
    assert "cue-bed" in quality["beds"]
    voices = {voice["voice_id"] for voice in rendered.show_notes["voices"]}
    assert voices == {"en-Mara_woman", "en-Pax_man"}
    assert rendered.speech_synthesized is False
    assert (tmp_path / "spectrogram.png").stat().st_size > 1000
    assert (tmp_path / "cube.png").stat().st_size > 1000


def test_publish_consent_and_disclosure_stop_the_episode():
    payload = _body()
    call_only = json.loads(json.dumps(payload["marks"][0]))
    call_only["consent"]["scope"] = "call"
    with pytest.raises(ConsentRefusal) as refused:
        render_episode(payload["rundown"], [call_only], mode="dry_run")
    assert refused.value.code == "consent_scope"
    assert refused.value.action == "publish"

    missing = json.loads(json.dumps(payload["marks"][0]))
    missing["opted_in"] = False
    missing["consent"]["opted_in"] = False
    missing["consent"]["scope"] = "none"
    missing["consent"]["disclosure_accepted"] = False
    with pytest.raises(ConsentRefusal) as missing_consent:
        require_publish_consent(missing)
    assert missing_consent.value.code == "consent_missing"
    require_call_consent(call_only)

    swapped = json.loads(json.dumps(payload["rundown"]))
    swapped["disclosure_text"] = "This recording notice was replaced."
    swapped["segments"][1]["text"] = swapped["disclosure_text"]
    with pytest.raises(Exception) as replaced:
        render_episode(swapped, payload["marks"], mode="dry_run")
    assert "cannot be replaced" in str(replaced.value)


def test_phone_numbers_are_refused_without_echoing_the_match():
    payload = _body()
    leaked = "415-555-0199"
    payload["rundown"]["segments"][4]["text"] = f"Call the desk at {leaked} tomorrow."
    status, body = handle_episode(payload)
    assert status == 409
    assert body["error"] == "phone_number_forbidden"
    blob = json.dumps(body)
    assert leaked not in blob
    assert "555" not in blob
    with pytest.raises(PhoneNumberRefusal):
        render_episode(payload["rundown"], payload["marks"], mode="dry_run")


def test_secret_fields_stay_out_of_the_error():
    payload = _body()
    payload["api_secret"] = "super-secret-value"
    status, body = handle_episode(payload)
    assert status == 400
    assert "super-secret-value" not in json.dumps(body)
    assert "environment" in body["message"]


def test_voice_paths_must_stay_inside_the_library():
    payload = _body()
    payload["rundown"]["segments"][2]["voice"]["relative_path"] = "../secrets.wav"
    with pytest.raises(Exception, match="relative to the voice library"):
        render_episode(payload["rundown"], payload["marks"], mode="dry_run")


def test_render_mode_refuses_without_vibevoice_weights(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GEN_AUDIO_VIBEVOICE_MODEL", raising=False)
    monkeypatch.delenv("GENAID_VOICES_DIR", raising=False)
    payload = _body()
    with pytest.raises(EngineRefusal) as refused:
        render_episode(
            payload["rundown"],
            payload["marks"],
            mode="render",
            output_dir=tmp_path,
            seed=1,
        )
    assert refused.value.engine == "vibevoice"
    assert "No speech was invented" in str(refused.value)
    assert list(tmp_path.iterdir()) == []


def test_multispeaker_commentary_script_and_render(tmp_path: Path, monkeypatch):
    lines = [
        {
            "speaker": "Mara",
            "text": "Welcome to the lobby.",
            "voice": {
                "voice_id": "en-Mara_woman",
                "relative_path": "vibevoice/en-Mara_woman.wav",
            },
        },
        {
            "speaker": "Pax",
            "text": "The lamp schedule is the joke.",
            "voice": {
                "voice_id": "en-Pax_man",
                "relative_path": "vibevoice/en-Pax_man.wav",
            },
        },
    ]
    plan = commentary_script(lines)
    assert plan["script"].splitlines() == [
        "Speaker 1: Welcome to the lobby.",
        "Speaker 2: The lamp schedule is the joke.",
    ]
    assert [row["voice_id"] for row in plan["speakers"]] == ["en-Mara_woman", "en-Pax_man"]
    dry, meta = render_multispeaker(lines, 24_000, mode="dry_run")
    assert meta["speech_synthesized"] is False
    assert dry.size > 1000

    library = tmp_path / "voices"
    for name in ("en-Mara_woman.wav", "en-Pax_man.wav"):
        destination = library / "vibevoice" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _tone(destination)
    monkeypatch.setenv("GENAID_VOICES_DIR", str(library))
    seen: dict[str, int] = {}

    def fake_probe(engine: str) -> Probe:
        return Probe(engine, True, [])

    def fake_synthesize(engine, turns, cast, output):
        seen["turns"] = len(turns)
        seen["speakers"] = len(cast.speakers)
        write_wav(output, np.full(4800, 0.1, dtype=np.float32), 24_000)
        return {"engine": engine, "sample_rate": 24000, "samples": 4800, "turns": len(turns)}

    monkeypatch.setattr("gen_audio.adapters.probe", fake_probe)
    monkeypatch.setattr("gen_audio.adapters.synthesize", fake_synthesize)
    audio, rendered = render_multispeaker(lines, 24_000, mode="render")
    assert rendered["speech_synthesized"] is True
    assert seen == {"turns": 2, "speakers": 2}
    assert audio.size == 4800
    with pytest.raises(Exception, match="at most 4"):
        commentary_script(
            [
                {"speaker": name, "text": "Line.", "voice": {"voice_id": f"v{index}"}}
                for index, name in enumerate("ABCDE")
            ]
        )


def test_renderer_protocol_dry_run_is_a_tone():
    voice = SimpleNamespace(
        voice_id="en-Mara_woman",
        provider="vibevoice",
        library="genaid-voices",
        relative_path=None,
    )
    line = SimpleNamespace(text="Hello from the host booth.", voice=voice, speaker="Mara")
    renderer = GenAudioVoiceoverRenderer(mode="dry_run")
    audio = renderer.render(line, 24_000)
    assert renderer.speech_synthesized is False
    assert audio.size > 100
    other = renderer.render(
        SimpleNamespace(text="Different chair.", voice=SimpleNamespace(voice_id="en-Pax_man", relative_path=None), speaker="Pax"),
        24_000,
    )
    assert not np.allclose(audio[:100], other[:100])


def test_core_boundary_delegates_when_installed_and_reports_absence(monkeypatch):
    reset_core_cache()
    monkeypatch.setattr("gen_audio.agentyankers.core.import_core", lambda: None)
    status = core_status()
    assert status["available"] is False
    assert status["schema_version"] == SCHEMA_VERSION
    schemas = export_json_schemas()
    assert schemas["EpisodeRundown"]["properties"]["schema_version"]["const"] == SCHEMA_VERSION

    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, voiceover, catalog=None):
            captured["voiceover"] = voiceover
            captured["catalog"] = catalog

        def assemble(self, request, bank):
            captured["request"] = request
            captured["bank"] = bank
            return {"assembled": True}

    fake = SimpleNamespace(EpisodePipeline=FakePipeline)
    monkeypatch.setattr("gen_audio.agentyankers.core.import_core", lambda: fake)
    renderer = GenAudioVoiceoverRenderer(mode="dry_run")
    result = assemble_with_core({"episode_id": "ep-quiet-lobby"}, bank="bank", renderer=renderer)
    assert result == {"assembled": True}
    assert captured["voiceover"] is renderer
    assert captured["bank"] == "bank"
    monkeypatch.setattr(
        "gen_audio.agentyankers.core.import_core",
        lambda: SimpleNamespace(export_json_schemas=lambda: {"EpisodeRundown": {"title": "core"}}),
    )
    assert export_json_schemas()["EpisodeRundown"]["title"] == "core"


def test_named_recording_is_mixed_and_paths_stay_in_the_artifact_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEN_AUDIO_ARTIFACT_DIR", str(tmp_path))
    _tone(tmp_path / "clip.WAV")
    payload = _body()
    payload["output_dir"] = "episodes/first"
    payload["recordings"] = {"rec-desk-moss": "clip.WAV"}
    status, body = dispatch("POST", "/genaid-audio/agentyankers/episode", body=payload)
    assert status == 200
    assert body["speech_synthesized"] is False
    notes = json.loads((tmp_path / "episodes" / "first" / "quality.json").read_text(encoding="utf-8"))
    assert all("call clip placeholder" not in item for item in notes["asset_warnings"])
    payload["recordings"] = {"rec-desk-moss": "unused/clip.WAV"}
    status, body = dispatch("POST", "/genaid-audio/agentyankers/episode", body=payload)
    assert (status, body["error"]) == (409, "missing_recording")
    outside = tmp_path / "outside"
    outside.mkdir()
    _tone(outside / "foreign.wav")
    payload["recordings"] = {"rec-desk-moss": str(outside / "foreign.wav")}
    status, body = dispatch("POST", "/genaid-audio/agentyankers/episode", body=payload)
    assert status == 409
    assert body["error"] == "missing_recording"
    assert "foreign.wav" not in json.dumps(body)
    assert str(outside) not in json.dumps(body)


def test_recording_link_must_resolve_inside_artifact_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setenv("GEN_AUDIO_ARTIFACT_DIR", str(root))
    tone = tmp_path / "tone.wav"
    _tone(tone)
    try:
        (root / "clip.wav").symlink_to(tone)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    payload = _body()
    payload["recordings"] = {"rec-desk-moss": "clip.wav"}
    status, body = handle_episode(payload)
    assert status == 400
    assert str(tmp_path) not in json.dumps(body)
    assert not (root / "episode").exists()


def test_duration_rules_and_vibevoice_env_name():
    from gen_audio.agentyankers.schema import _duration, validate_rundown

    assert ENV_VIBEVOICE_MODEL == "GEN_AUDIO_VIBEVOICE_MODEL"
    assert _duration(None, "cue", required=False) is None
    assert _duration(1.5, "cue", required=False) == 1.5
    with pytest.raises(Exception, match="duration"):
        _duration(None, "clip", required=True)
    payload = _body()["rundown"]
    commentary = json.loads(json.dumps(payload))
    commentary["segments"][2].pop("duration_seconds")
    validate_rundown(commentary)
    missing = json.loads(json.dumps(payload))
    missing["segments"][4].pop("duration_seconds")
    with pytest.raises(Exception, match="duration_seconds"):
        validate_rundown(missing)
    optional_null = json.loads(json.dumps(payload))
    optional_null["cues"][0]["duration_seconds"] = None
    validate_rundown(optional_null)


def test_engine_registry_names_the_podcast_format():
    spec = get_engine("agentyankers")
    assert spec.kind == "podcast"
    assert spec.status == "format"
    assert "GENAID_VOICES_DIR" in spec.summary


def test_serve_route_dry_run_and_guards(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEN_AUDIO_ARTIFACT_DIR", str(tmp_path))
    payload = _body()
    status, body = dispatch("POST", "/genaid-audio/agentyankers/episode", body=payload)
    assert status == 200
    assert body["speech_synthesized"] is False
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["files"]["audio"] == "episode.wav"
    assert (tmp_path / "episode" / "episode.wav").is_file()
    assert "agent:desk-moss" not in json.dumps(body["quality"])
    assert agentyankers_episode_url("127.0.0.1").endswith("/genaid-audio/agentyankers/episode")

    described, descriptor = dispatch("GET", "/genaid-audio/agentyankers")
    assert described == 200
    assert descriptor["modes"] == ["dry_run", "render"]
    assert descriptor["disclosure"] == DISCLOSURE

    payload["output_dir"] = "../escape"
    status, body = dispatch("POST", "/genaid-audio/agentyankers/episode", body=payload)
    assert status == 400
    assert body["speech_synthesized"] is False

    health, health_body = dispatch("GET", "/genaid-audio/health", model_loaded=False)
    assert health == 200
    assert health_body["service"] == "genaid-audio"
    refused, method = dispatch("POST", "/genaid-audio/health", body={})
    assert refused == 405
    assert method["error"] == "method not allowed"


def test_loopback_post_dry_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEN_AUDIO_ARTIFACT_DIR", str(tmp_path))
    server = serve_loopback("127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        raw = json.dumps(_body()).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{port}/genaid-audio/agentyankers/episode",
            data=raw,
            headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            body = json.load(response)
            assert response.status == 200
        assert body["mode"] == "dry_run"
        assert (tmp_path / "episode" / "quality.json").is_file()
    finally:
        server.shutdown()
        server.server_close()
