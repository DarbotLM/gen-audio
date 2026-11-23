"""Yumlog Studio AgentVoice import (gen_audio.agent_voices) and the committed catalog."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from gen_audio import agent_voices as av
from gen_audio.audio_io import write_wav
from gen_audio.cli import agent_voices as cli
from gen_audio.identity import mint

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "apps" / "desktop" / "public" / "library" / "agentvoice" / "assets.json"


def _voice_wav(path: Path, seconds: float, f0: float, rate: int = 16000) -> str:
    t = np.arange(int(seconds * rate)) / rate
    audio = 0.3 * np.sin(2 * np.pi * f0 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)) + 0.01 * np.random.default_rng(1).standard_normal(len(t))
    write_wav(path, audio, rate)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "yumlog"
    src.mkdir()
    sha_a = _voice_wav(src / "practical_explorer.wav", 2.5, 180.0)
    sha_b = _voice_wav(src / "curious_builder.wav", 0.4, 240.0)
    _voice_wav(src / "focused_navigator.wav", 1.0, 120.0)
    voices = [
        {"id": "speaker-01", "alias": "practical_explorer", "name": "practical_explorer.wav", "sha256": sha_a, "durationSeconds": 2.5,
         "artifactId": "a" * 64, "turns": [{"startSeconds": 1.0, "endSeconds": 2.0}, {"startSeconds": 10.0, "endSeconds": 11.5}]},
        {"id": "speaker-02", "alias": "curious_builder", "name": "curious_builder.wav", "sha256": sha_b, "durationSeconds": 0.4,
         "turns": [{"startSeconds": 5.0, "endSeconds": 5.4}]},
        {"id": "speaker-03", "alias": "thoughtful_guide", "name": "thoughtful_guide.wav", "sha256": "c" * 64, "durationSeconds": 3.0,
         "turns": [{"startSeconds": 20.0, "endSeconds": 23.0}]},
        {"id": "speaker-04", "alias": "focused_navigator", "name": "focused_navigator.wav", "sha256": "d" * 64, "durationSeconds": 1.0,
         "turns": [{"startSeconds": 30.0, "endSeconds": 31.0}]},
    ]
    doc = {"schema": av.YUMLOG_SCHEMA, "method": "sherpa-onnx test", "sourceSha256": "e" * 64, "durationSeconds": 40.0, "voices": voices}
    (src / "agent-voices.json").write_text(json.dumps(doc), encoding="utf-8")
    return src


def test_wrong_schema_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "agent-voices.json").write_text(json.dumps({"schema": "urn:other", "voices": []}), encoding="utf-8")
    with pytest.raises(av.AgentVoiceSourceError, match="schema"):
        av.load_source(tmp_path / "agent-voices.json")


def test_import_keeps_every_source_voice_and_derives_real_media(tmp_path: Path) -> None:
    library = tmp_path / "library"
    imported = av.import_set(_source(tmp_path), "unit-set", library)
    rows = {row["alias"]: row for row in imported["voices"]}
    assert imported["source"]["voice_count"] == 4
    assert len(rows) == 4
    assert {alias for alias, row in rows.items() if row["status"] == "ok"} == {"practical_explorer", "curious_builder"}
    assert "not in the imported folder" in rows["thoughtful_guide"]["reason"]
    assert "does not match the sha256" in rows["focused_navigator"]["reason"]
    by_uid = {asset["uid"]: asset for asset in imported["assets"]}
    for alias in ("practical_explorer", "curious_builder"):
        row = rows[alias]
        clip = by_uid[row["clip_uid"]]
        assert clip["honesty"]["synthesized_speech"] is False and clip["honesty"]["claims"] == ["real_wav"]
        assert by_uid[row["spectrogram_uid"]]["src"] == [clip["uid"]]
        assert by_uid[row["cube_uid"]]["src"] == [clip["uid"]]
        assert by_uid[row["cube_uid"]]["fields"]["source_sha256"] == clip["media"][0]["sha256"]
        extension = clip["extensions"][av.EXTENSION]
        assert extension["appearance_count"] == len(extension["appearances"]) == row["appearance_count"]
        segments = clip["body"]["segments"]
        assert segments[-1]["end_s"] == pytest.approx(sum(a["end_s"] - a["start_s"] for a in extension["appearances"]))
        for item in clip["media"]:
            assert (library / item["path"]).is_file()
    assert [a for a in imported["assets"] if rows["thoughtful_guide"].get("clip_uid") == a["uid"]] == []
    path = av.write_catalog([imported], library)
    assert av.check_catalog(json.loads(path.read_text(encoding="utf-8")), library, require_wavs=True) == []


def test_uids_re_mint_and_glyphs_follow_the_digest(tmp_path: Path) -> None:
    imported = av.import_set(_source(tmp_path), "unit-set", tmp_path / "library")
    for asset in imported["assets"]:
        digests = [{"role": m["role"], "sha256": m["sha256"]} for m in asset["media"]]
        assert mint(asset["kind"], asset["fields"], digests, asset["src"]) == asset["uid"]
        digest_glyph = av.glyph_from_uid(asset["uid"])
        assert asset["display"]["glyph"] == digest_glyph and len(digest_glyph) == 2


def test_long_voices_get_a_bounded_strip() -> None:
    assert av.strip_params(60.0).columns_per_second == 10
    assert av.strip_params(2979.1).columns_per_second == 2
    assert av.strip_params(2979.1).columns_per_second * 2979.1 <= av.MAX_STRIP_COLUMNS


def test_cli_import_then_check(tmp_path: Path, capsys) -> None:
    library = tmp_path / "library"
    assert cli.main(["import", str(_source(tmp_path)), "--set", "unit-set", "--library", str(library)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["source_voices"] == summary["imported_rows"] == 4
    assert summary["with_audio"] == summary["with_spectrogram"] == summary["with_cube"] == 2
    assert cli.main(["check", "--library", str(library)]) == 0


def test_committed_catalog_imports_every_yumlog_voice() -> None:
    doc = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert doc["sources"], "no AgentVoice set imported"
    for source in doc["sources"]:
        rows = [row for row in doc["voices"] if row["set"] == source["set"]]
        assert len(rows) == source["voice_count"]
    assert av.check_catalog(doc) == []


def test_committed_catalog_every_voice_with_audio_has_spectrogram_and_cube() -> None:
    doc = json.loads(CATALOG.read_text(encoding="utf-8"))
    kinds = {asset["uid"]: asset["kind"] for asset in doc["assets"]}
    with_audio = [row for row in doc["voices"] if row["status"] == "ok"]
    assert with_audio
    for row in with_audio:
        assert kinds[row["clip_uid"]] == "audio_clip"
        assert kinds[row["spectrogram_uid"]] == "spectrogram_2d", row["alias"]
        assert kinds[row["cube_uid"]] == "cube_ihdr", row["alias"]


def test_clip_body_is_one_resolved_speaker_inside_the_wav(tmp_path: Path) -> None:
    imported = av.import_set(_source(tmp_path), "unit-set", tmp_path / "library")
    clips = [a for a in imported["assets"] if a["kind"] == "audio_clip"]
    assert clips
    for clip in clips:
        body = clip["body"]
        assert [s["idx"] for s in body["speakers"]] == [0]
        assert body["segments"], clip["uid"]
        assert all(s["speaker_idx"] == 0 and 0 <= s["start_s"] < s["end_s"] <= body["duration_s"] + 1e-6 for s in body["segments"])


def test_derived_coverage_never_exceeds_the_wav(tmp_path: Path) -> None:
    imported = av.import_set(_source(tmp_path), "unit-set", tmp_path / "library")
    derived = [a for a in imported["assets"] if a["kind"] in ("cube_ihdr", "spectrogram_2d")]
    assert derived
    for asset in derived:
        assert asset["fields"]["covers_ms"] <= asset["fields"]["duration_ms"], asset["uid"]


def test_written_json_is_lf_only(tmp_path: Path) -> None:
    # Media hashes are over the written bytes and the repo stores *.json as LF;
    # a CRLF write on Windows would make every committed hash wrong elsewhere.
    library = tmp_path / "library"
    av.write_catalog([av.import_set(_source(tmp_path), "unit-set", library)], library)
    written = list((library / "agentvoice").rglob("*.json"))
    assert len(written) >= 3
    for path in written:
        assert b"\r\n" not in path.read_bytes(), path
