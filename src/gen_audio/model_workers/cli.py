"""CLI protocol for isolated VibeVoice and PersonaPlex workers."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path

from .contracts import MAX_TEXT_BYTES, Request, WorkerError
from .preflight import local_file, preflight
from .runtime import execute


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise WorkerError("invalid_arguments", message)


def parser() -> argparse.ArgumentParser:
    result = Parser(description=__doc__)
    result.add_argument("--engine", choices=("vibevoice", "personaplex"), required=True)
    result.add_argument("--model-dir", type=Path, required=True)
    result.add_argument("--tokenizer", type=Path)
    result.add_argument("--output", type=Path)
    text = result.add_mutually_exclusive_group()
    text.add_argument("--text")
    text.add_argument("--input-text", type=Path)
    result.add_argument("--input-audio", type=Path)
    result.add_argument("--persona")
    result.add_argument("--voice-ref", type=Path, action="append", default=[])
    result.add_argument("--device-index", type=int, default=0)
    result.add_argument("--execution", help="JSON hybrid execution options")
    result.add_argument("--max-new-tokens", type=int, default=256)
    result.add_argument("--max-seconds", type=float, default=30.0)
    result.add_argument("--timeout-seconds", type=float, default=600.0)
    result.add_argument("--seed", type=int, default=0)
    result.add_argument("--cfg-scale", type=float, default=1.3)
    result.add_argument("--inference-steps", type=int, default=10)
    result.add_argument("--preflight", action="store_true")
    result.add_argument("--load-only", action="store_true", help="Validate load prerequisites without generation fields")
    return result


def request_from_args(args) -> Request:
    text = args.text
    if args.input_text is not None:
        path = local_file(args.input_text, max_bytes=MAX_TEXT_BYTES)
        text = path.read_bytes()[:MAX_TEXT_BYTES].decode("utf-8-sig")
    return Request(
        engine=args.engine,
        model_dir=args.model_dir,
        tokenizer=args.tokenizer,
        output=args.output,
        text=text,
        input_audio=args.input_audio,
        persona=args.persona,
        voice_refs=tuple(args.voice_ref),
        device_index=args.device_index,
        max_new_tokens=args.max_new_tokens,
        max_seconds=args.max_seconds,
        timeout_seconds=args.timeout_seconds,
        seed=args.seed,
        cfg_scale=args.cfg_scale,
        inference_steps=args.inference_steps,
        execution=json.loads(args.execution) if args.execution is not None else None,
    )


def main(argv: list[str] | None = None) -> int:
    engine = None
    try:
        args = parser().parse_args(argv)
        engine = args.engine
        request = request_from_args(args)
        with redirect_stdout(sys.stderr):
            receipt = preflight(
                request,
                load_only=args.load_only or args.preflight,
            ) if (args.preflight or args.load_only) else execute(request)
        print(json.dumps(receipt, allow_nan=False))
        return 0 if receipt["ok"] else 2
    except (WorkerError, OSError, ValueError, RuntimeError, ImportError) as exc:
        if not isinstance(exc, WorkerError):
            traceback.print_exc(file=sys.stderr)
        receipt = {
            "schema_version": "1.0",
            "ok": False,
            "synthesizedSpeech": False,
            "engine": engine,
            "errors": [
                {
                    "code": getattr(exc, "code", "worker_failed"),
                    "message": str(exc)
                    if isinstance(exc, WorkerError)
                    else f"Local worker failed ({type(exc).__name__}): {str(exc)[:2000]}",
                }
            ],
        }
        print(json.dumps(receipt, allow_nan=False))
        return 2

