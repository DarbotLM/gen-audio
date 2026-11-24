"""Offline execution fence, audio validation, and durable receipt publication."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import wave
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from .contracts import SAMPLE_RATE, Request, WorkerError
from .preflight import preflight


@contextmanager
def offline_runtime():
    def deny(*args, **kwargs):
        raise WorkerError("network_forbidden", "Network access is disabled in model workers")

    flags = {
        key: "1"
        for key in (
            "HF_HUB_OFFLINE",
            "TRANSFORMERS_OFFLINE",
            "HF_DATASETS_OFFLINE",
            "HF_HUB_DISABLE_TELEMETRY",
            "DO_NOT_TRACK",
            "GENAID_LOCAL_FILES_ONLY",
        )
    }
    flags["TOKENIZERS_PARALLELISM"] = "false"
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, flags))
        for name in ("connect", "connect_ex", "sendto", "sendall", "send"):
            stack.enter_context(patch.object(socket.socket, name, deny))
        stack.enter_context(patch.object(socket, "create_connection", deny))
        stack.enter_context(patch.object(socket, "getaddrinfo", deny))
        yield


def check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise WorkerError("generation_timeout", "Model worker exceeded its cooperative deadline")


def checked_audio(audio, *, max_seconds: float, allow_silence: bool = False):
    import numpy as np

    if hasattr(audio, "detach"):
        audio = audio.detach().float().cpu().numpy()
    values = np.asarray(audio)
    if values.ndim == 2 and values.shape[0] == 1:
        values = values[0]
    if values.ndim != 1 or values.size == 0 or values.size > SAMPLE_RATE * max_seconds:
        raise WorkerError("invalid_generated_audio", "Expected mono audio within the duration cap")
    if values.dtype.kind != "f" or not np.isfinite(values).all():
        raise WorkerError("invalid_generated_audio", "Audio must contain only finite floating-point samples")
    if not allow_silence and not np.any(values):
        raise WorkerError("invalid_generated_audio", "Model produced only silence")
    return np.ascontiguousarray(values, dtype=np.float32)


def read_pcm(
    path: Path,
    *,
    seconds: float,
    allow_silence: bool = False,
    resample: bool = False,
):
    import numpy as np

    from .preflight import inspect_wav

    info = inspect_wav(
        path,
        maximum=seconds,
        allowed_rates=(16_000, 22_050, 24_000, 44_100, 48_000)
        if resample
        else (SAMPLE_RATE,),
    )
    with wave.open(str(path), "rb") as handle:
        values = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if info["sample_rate"] != SAMPLE_RATE:
        from math import gcd

        from scipy.signal import resample_poly

        factor = gcd(info["sample_rate"], SAMPLE_RATE)
        values = resample_poly(
            values,
            SAMPLE_RATE // factor,
            info["sample_rate"] // factor,
        ).astype(np.float32)
    return checked_audio(values, max_seconds=seconds, allow_silence=allow_silence)


def _replace_staged(staged: Path, destination: Path) -> None:
    os.replace(staged, destination)


def publish(request: Request, audio, provenance: dict) -> dict:
    import numpy as np

    if request.output is None:
        raise WorkerError("missing_output", "Generation requires an output WAV")
    output = request.output
    manifest = output.with_suffix(".synth.json")
    for destination in (output, manifest):
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"Refusing to overwrite {destination}")
    output.parent.mkdir(parents=True, exist_ok=True)
    audio = checked_audio(audio, max_seconds=request.max_seconds)
    pcm = np.rint(np.clip(audio, -1, 1) * 32767).astype("<i2")
    if not np.any(pcm):
        raise WorkerError("invalid_generated_audio", "Output quantizes to silence")
    token = uuid4().hex
    staged_wav = output.with_name(f".{output.name}.{token}.partial")
    staged_manifest = manifest.with_name(f".{manifest.name}.{token}.partial")
    receipt = {
        "schema_version": "1.0",
        "ok": True,
        "synthesizedSpeech": True,
        "engine": request.engine,
        "output": str(output),
        "manifest": str(manifest),
        "sample_rate": SAMPLE_RATE,
        "samples": int(pcm.size),
        "duration_s": pcm.size / SAMPLE_RATE,
    }
    record = {
        **receipt,
        "task": "speech_to_speech" if request.engine == "personaplex" else "script_tts",
        "transcript_verified": False,
        "output_semantics": (
            "generated_conversation_speech"
            if request.engine == "personaplex"
            else "generated_script_speech"
        ),
        "clipped_samples": int(np.count_nonzero(np.abs(audio) > 1)),
        "peak": float(np.max(np.abs(audio))),
        "rms": float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))),
        "provenance": provenance,
    }
    try:
        with wave.open(str(staged_wav), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(pcm.tobytes())
        with wave.open(str(staged_wav), "rb") as handle:
            if handle.getnframes() != pcm.size or handle.getframerate() != SAMPLE_RATE:
                raise WorkerError("publication_failed", "Written WAV failed verification")
        receipt["sha256"] = hashlib.sha256(staged_wav.read_bytes()).hexdigest()
        record["sha256"] = receipt["sha256"]
        staged_manifest.write_text(
            json.dumps(record, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        _replace_staged(staged_wav, output)
        _replace_staged(staged_manifest, manifest)
    finally:
        staged_wav.unlink(missing_ok=True)
        staged_manifest.unlink(missing_ok=True)
    return receipt


def execute(request: Request, *, resident: dict | None = None) -> dict:
    report = preflight(request, resident=resident)
    if not report["ready"]:
        return report
    deadline = time.monotonic() + request.timeout_seconds
    started = time.perf_counter()
    with offline_runtime():
        if request.engine == "vibevoice":
            from .vibevoice import generate
        else:
            from .personaplex import generate
        audio, details = generate(request, report, deadline, resident=resident)
    check_deadline(deadline)
    elapsed = time.perf_counter() - started
    return publish(
        request,
        audio,
        {
            "runtime_revision": report["runtime"]["revision"],
            "runtime_repository": report["runtime"]["repository"],
            "runtime_requirements": report["runtime"]["requirements"],
            "model_dir": report["model_dir"],
            "weights": report["assets"]["weights"],
            "execution": report["execution"],
            "offline": True,
            "timing": {
                "generation_seconds": elapsed,
                "includes_model_load": resident is None,
                "real_time_factor": elapsed / (audio.size / SAMPLE_RATE),
            },
            "seed": request.seed,
            **details,
        },
    )
