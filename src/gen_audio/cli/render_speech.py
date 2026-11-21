"""Render one authored utterance and print its WAV facts for other tools."""

import argparse
import hashlib
import json
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
    args = parser.parse_args()
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
