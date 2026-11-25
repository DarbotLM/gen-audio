"""Control and call the resident VibeVoice 7B / PersonaPlex 7B workers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from gen_audio.model_clients import (
    generate_worker,
    load_worker,
    preflight_worker,
    unload_worker,
    worker_status,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("status", "preflight", "load", "unload"):
        item = sub.add_parser(command)
        item.add_argument("--engine", choices=("vibevoice", "personaplex"), required=True)
    vibe = sub.add_parser("vibevoice")
    vibe.add_argument("--text")
    vibe.add_argument("--input-text", type=Path)
    vibe.add_argument("--voice-ref", action="append", required=True)
    _generation_args(vibe)
    vibe.add_argument("--cfg-scale", type=float, default=1.3)
    vibe.add_argument("--inference-steps", type=int, default=10)
    persona = sub.add_parser("personaplex")
    persona.add_argument("--input-audio", required=True)
    persona.add_argument("--voice-ref", required=True)
    persona.add_argument("--persona", required=True)
    _generation_args(persona)
    return root


def _generation_args(command) -> None:
    command.add_argument("--output")
    command.add_argument("--max-new-tokens", type=int, default=256)
    command.add_argument("--max-seconds", type=float, default=30)
    command.add_argument("--timeout-seconds", type=float, default=1_200)
    command.add_argument("--seed", type=int, default=0)
    command.add_argument("--publish", action="store_true")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            result = worker_status(args.engine)
        elif args.command == "preflight":
            result = preflight_worker(args.engine)
        elif args.command == "load":
            result = load_worker(args.engine)
        elif args.command == "unload":
            result = unload_worker(args.engine)
        elif args.command == "vibevoice":
            text = args.text
            if args.input_text is not None:
                if text is not None:
                    raise ValueError("Use either --text or --input-text")
                text = args.input_text.read_text(encoding="utf-8-sig")
            result = generate_worker(
                "vibevoice",
                {
                    "scriptText": text,
                    "voiceRefs": args.voice_ref,
                    **_common(args),
                    "cfgScale": args.cfg_scale,
                    "inferenceSteps": args.inference_steps,
                },
                local_output=Path(args.output) if args.output else None,
            )
        else:
            result = generate_worker(
                "personaplex",
                {
                    "inputAudio": args.input_audio,
                    "voiceRefs": [args.voice_ref],
                    "persona": args.persona,
                    **_common(args),
                },
                local_output=Path(args.output) if args.output else None,
            )
        if args.command in {"vibevoice", "personaplex"} and args.publish:
            result["published"] = publish_receipt(result)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "errors": [{"code": "client_failed", "message": str(exc)}]}))
        return 2
    print(json.dumps(result, indent=2))
    return 0


def _common(args) -> dict:
    result = {
        "maxNewTokens": args.max_new_tokens,
        "maxSeconds": args.max_seconds,
        "timeoutSeconds": args.timeout_seconds,
        "seed": args.seed,
    }
    if args.output:
        result["output"] = args.output
    return result


def publish_receipt(receipt: dict) -> dict:
    from gen_audio.audio_io import read_wav, write_wav
    from gen_audio.improve import improve

    raw = Path(receipt["output"])
    audio, rate = read_wav(raw)
    result = improve(audio, rate, target_sr=24_000)
    output = raw.with_name(f"{raw.stem}-published.wav")
    manifest = raw.with_suffix(".pipeline.json")
    if output.exists() or manifest.exists():
        raise ValueError(f"refusing to overwrite publish artifacts for {raw.name}")
    write_wav(output, result.audio, result.sample_rate)
    payload = {
        "schema_version": "1.0",
        "ok": True,
        "engine": receipt["engine"],
        "source": str(raw),
        "output": str(output),
        "sample_rate": result.sample_rate,
        "samples": int(result.audio.size),
        "duration_s": float(result.output_seconds),
        "peak": float(result.peak),
        "input_sample_rate": result.input_sample_rate,
        "input_duration_s": float(result.input_seconds),
        "chain": ["trim", "highpass", "peak_normalize", "resample_24khz", "pcm16"],
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
