"""Run a locally installed VibeVoice ASR checkpoint."""

import argparse
import json
from pathlib import Path

from gen_audio.audio_io import read_wav
from gen_audio.vibevoice_asr import VibeVoiceASR


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--context")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--max-new-tokens", type=int)
    parser.add_argument("--streaming", action="store_true", help="Emit checkpoint-trained chunks as JSON lines")
    args = parser.parse_args()
    if not args.model.is_dir() or (args.max_new_tokens is not None and args.max_new_tokens < 1):
        parser.error("provide a local checkpoint directory and a positive token limit")

    samples, rate = read_wav(args.audio)
    if not samples.size:
        parser.error("audio file is empty")
    recognizer = VibeVoiceASR(args.model, streaming=args.streaming, device=args.device)
    if args.streaming:
        for chunk in recognizer.stream(samples, rate, context=args.context, max_tokens=args.max_new_tokens or 256):
            print(json.dumps(chunk), flush=True)
    else:
        print(json.dumps(recognizer.transcribe(samples, rate, context=args.context, max_tokens=args.max_new_tokens or 512)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
