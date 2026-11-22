"""CLI for prompt → synth → analysis. Prints one JSON object on stdout."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

from gen_audio.adapters import EngineRefusal, synthesize
from gen_audio.assets import asset_object
from gen_audio.cast import load_cast_map, read_script
from gen_audio.pipeline import run_pipeline
from gen_audio.prompt_script import PromptError, prepare_prompt, write_prepared
from gen_audio.speakers import SpeakerError, timeline_from_dict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a clip from a user prompt and run the analysis pipeline.")
    parser.add_argument("--prompt-file", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--personas", help="Comma-separated persona ids, at most 8")
    group.add_argument("--cast-map", type=Path, help="Authored cast map inside the work directory")
    parser.add_argument("--engine", required=True)
    parser.add_argument("--duration-s", type=float, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--provider")
    parser.add_argument("--vocab")
    parser.add_argument("--trim", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--continuous", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--misaki-g2p", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--sentence-pause", type=float)
    parser.add_argument("--clause-pause", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides = {
        "ONNX_PROVIDER": args.provider,
        "GEN_AUDIO_KOKORO_VOCAB": args.vocab,
        "GEN_AUDIO_KOKORO_TRIM": args.trim,
        "GEN_AUDIO_KOKORO_CONTINUOUS": args.continuous,
        "GEN_AUDIO_KOKORO_USE_MISAKI": args.misaki_g2p,
        "GEN_AUDIO_KOKORO_SENTENCE_PAUSE": args.sentence_pause,
        "GEN_AUDIO_KOKORO_CLAUSE_PAUSE": args.clause_pause,
    }
    for key, value in overrides.items():
        if value is not None:
            os.environ[key] = (
                str(value).lower() if isinstance(value, bool) else str(value)
            )
    out_dir = args.out_dir.resolve()
    prompt_path = args.prompt_file.resolve()
    try:
        prompt_path.relative_to(out_dir)
    except ValueError:
        _emit(out_dir, {"ok": False, "synthesizedSpeech": False, "reason": "prompt file is outside the work directory"})
        return 2
    personas = [part.strip() for part in (args.personas or "").split(",") if part.strip()]
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
        if not 0.5 <= args.duration_s <= 1800:
            raise ValueError("duration must be from 0.5 to 1800 seconds")
        if args.cast_map:
            cast_path = args.cast_map.resolve()
            cast_path.relative_to(out_dir)
            script_path = prompt_path
            prepared = None
        else:
            prepared = prepare_prompt(prompt, personas, args.engine, args.duration_s)
            script_path = out_dir / "scripts" / "script.txt"
            cast_path = out_dir / "cast.json"
            write_prepared(prepared, script_path, cast_path)
        script_asset = asset_object(script_path, kind="script", derived_from=[], duration_s=None)
        turns = read_script(script_path)
        cast = load_cast_map(cast_path)
        spoken_text = " ".join(turn.text for turn in turns)
        raw_path = out_dir / "raw.wav"
        with redirect_stdout(sys.stderr):
            rendered = synthesize(args.engine, turns, cast, raw_path)
        speech = timeline_from_dict(rendered["speech"]) if isinstance(rendered, dict) and rendered.get("speech") else None
        manifest = run_pipeline(
            raw_path,
            out_dir=out_dir,
            engine=args.engine,
            reference_text=spoken_text,
            duration_target_s=args.duration_s,
            script_asset=script_asset,
            speech=speech,
        )
        manifest["spokenText"] = spoken_text
        manifest["speed"] = prepared.speed if prepared else cast.default_speed
        manifest["castSource"] = "persona-voice-map" if prepared else "authored"
        manifest["sampleScript"] = False
        if isinstance(rendered, dict):
            if rendered.get("providers"):
                manifest["onnxProviders"] = rendered["providers"]
            if rendered.get("phoneme_timings"):
                manifest["phonemeTimings"] = rendered["phoneme_timings"]
    except (PromptError, SpeakerError, EngineRefusal, OSError, ValueError, KeyError, RuntimeError) as exc:
        _emit(out_dir, {
            "ok": False,
            "synthesizedSpeech": False,
            "synthesized": False,
            "engine": args.engine,
            "reason": str(exc),
            "sampleScript": False,
        })
        return 0
    _emit(out_dir, manifest)
    return 0


def _emit(out_dir: Path, manifest: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    summary = {
        "ok": bool(manifest.get("ok")),
        "synthesizedSpeech": manifest.get("synthesizedSpeech") is True,
        "manifest": "manifest.json",
        "reason": manifest.get("reason", ""),
        "uid": (manifest.get("assets") or {}).get("wav", {}).get("uid", ""),
        "duration_s": manifest.get("duration_s"),
        "durationHonoured": manifest.get("durationHonoured"),
        "speech_s": manifest.get("speech_s"),
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    raise SystemExit(main())
