"""Render one authored utterance and print its WAV facts for other tools."""

import argparse
import hashlib
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

from gen_audio.adapters import synthesize
from gen_audio.cast import CastMap, Turn, VoiceAssignment
from gen_audio.guards import max_total_script_chars


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--language", default="en-us")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider")
    parser.add_argument("--vocab")
    parser.add_argument("--espeak-lib")
    parser.add_argument("--espeak-data")
    parser.add_argument("--trim", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--continuous", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--misaki-g2p", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--sentence-pause", type=float)
    parser.add_argument("--clause-pause", type=float)
    args = parser.parse_args()
    overrides = {
        "ONNX_PROVIDER": args.provider,
        "GEN_AUDIO_KOKORO_VOCAB": args.vocab,
        "GEN_AUDIO_KOKORO_ESPEAK_LIB": args.espeak_lib,
        "GEN_AUDIO_KOKORO_ESPEAK_DATA": args.espeak_data,
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
    limit = max_total_script_chars()
    try:
        text = sys.stdin.buffer.read(limit * 4 + 1).decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        parser.error("stdin must use UTF-8")
    if not text or len(text) > limit:
        parser.error("stdin must contain a nonempty utterance within the script limit")
    voice = VoiceAssignment("1", "Narrator", args.voice, args.language, 1.0)
    cast = CastMap(args.engine, args.language, 1.0, {"1": voice})
    try:
        with redirect_stdout(sys.stderr):
            result = synthesize(args.engine, [Turn("1", text, None, 1)], cast, args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    with args.output.open("rb") as audio:
        checksum = hashlib.file_digest(audio, "sha256").hexdigest()
    print(json.dumps({
        "engine": args.engine, "voice": args.voice, "sampleRate": result["sample_rate"],
        "durationSeconds": result["samples"] / result["sample_rate"], "audioSha256": checksum,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
