"""Bounded local-file, dependency, audio, RAM, and accelerator preflight."""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import struct
import sys
import wave
from importlib import metadata
from pathlib import Path

from .contracts import (
    GIB,
    MAX_JSON_BYTES,
    MAX_NEW_TOKENS,
    MAX_REFERENCE_SECONDS,
    MAX_SECONDS,
    MAX_TEXT_BYTES,
    MAX_TEXT_CHARS,
    SAMPLE_RATE,
    Request,
    WorkerError,
    runtime_spec,
)
from .execution import preferences, resolve


def _runtime_versions(spec: dict) -> dict[str, str]:
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        raise WorkerError("runtime_mismatch", "Use the isolated CPython 3.12 worker, not the app interpreter")
    try:
        from packaging.requirements import Requirement
    except ImportError as exc:
        raise WorkerError("missing_dependency", "Worker dependency validation requires packaging") from exc

    manifest = Path(__file__).resolve().parents[3] / spec["requirements"]
    try:
        lines = manifest.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise WorkerError("missing_runtime_manifest", f"Cannot read {manifest}") from exc
    versions = {}
    try:
        provider = metadata.distribution(spec["distribution"])
        origin = json.loads(provider.read_text("direct_url.json") or "{}")
        vcs = origin.get("vcs_info") if isinstance(origin, dict) else None
        if not isinstance(vcs, dict) or vcs.get("commit_id") != spec["revision"]:
            raise WorkerError("runtime_mismatch", f"{provider.name} must be installed from {spec['revision']}")
        # Check the provider's mandatory constraints as well: matching the top-level
        # pins alone does not make a --no-deps installation compatible.
        for source, entries in ((spec["requirements"], lines), (provider.name, provider.requires or [])):
            for line in entries:
                line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
                if not line or line.startswith(("#", "--extra-index-url ")):
                    continue
                req = Requirement(line)
                if req.marker and not req.marker.evaluate({"extra": ""}):
                    continue
                if req.url and (
                    req.name != spec["distribution"]
                    or not req.url.partition("#")[0].endswith("@" + spec["revision"])
                ):
                    raise WorkerError("runtime_mismatch", f"Unexpected provider reference in {source}: {req.name}")
                if req.name not in versions:
                    versions[req.name] = metadata.version(req.name)
                if req.specifier and not req.specifier.contains(versions[req.name], prereleases=True):
                    raise WorkerError("runtime_mismatch", f"{source} requires {req}; installed {versions[req.name]}")
    except metadata.PackageNotFoundError as exc:
        raise WorkerError("missing_dependency", f"Missing worker distribution: {exc.name}") from exc
    return versions


def read_json(path: Path) -> dict:
    path = local_file(path, max_bytes=MAX_JSON_BYTES)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise WorkerError("invalid_asset", f"Invalid JSON in {path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise WorkerError("invalid_asset", f"Expected a JSON object in {path}")
    return value


def local_file(path: Path, *, max_bytes: int | None = None) -> Path:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise WorkerError("missing_asset", f"Missing or empty local file: {path}")
    if path.is_symlink():
        raise WorkerError("invalid_asset", f"Refusing symlinked model input: {path}")
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise WorkerError("resource_limit", f"File exceeds {max_bytes} bytes: {path}")
    return path


def memory_snapshot() -> dict[str, int]:
    if os.name != "nt":
        raise WorkerError("memory_unknown", "This local hybrid profile supports Windows hosts")

    class Status(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("load", ctypes.c_ulong),
            ("total", ctypes.c_ulonglong),
            ("available", ctypes.c_ulonglong),
            ("total_page", ctypes.c_ulonglong),
            ("available_page", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("extended_virtual", ctypes.c_ulonglong),
        ]

    status = Status()
    status.length = ctypes.sizeof(Status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return {"total_bytes": int(status.total), "available_bytes": int(status.available)}


def require_host_memory(required: int) -> dict:
    snapshot = memory_snapshot()
    safe = max(0, min(snapshot["available_bytes"] - 2 * GIB, int(snapshot["total_bytes"] * 0.82)))
    report = {
        **snapshot,
        "required_bytes": required,
        "reserve_bytes": 2 * GIB,
        "safe_budget_bytes": safe,
        "sufficient": safe >= required,
    }
    if not report["sufficient"]:
        raise WorkerError(
            "insufficient_memory",
            f"Hybrid loading requires about {required / GIB:.1f} GiB; "
            f"only {snapshot['available_bytes'] / GIB:.1f} GiB is currently available",
        )
    return report


def inspect_wav(
    path: Path,
    *,
    maximum: float = MAX_SECONDS,
    minimum: float = 0.0,
    allowed_rates: tuple[int, ...] = (SAMPLE_RATE,),
) -> dict:
    path = local_file(path, max_bytes=int(SAMPLE_RATE * maximum * 2) + 65_536)
    if path.suffix.lower() != ".wav":
        raise WorkerError("unsupported_audio", "Only raw PCM WAV audio is accepted")
    try:
        with wave.open(str(path), "rb") as handle:
            if (
                handle.getframerate() not in allowed_rates
                or handle.getnchannels() != 1
                or handle.getsampwidth() != 2
                or handle.getcomptype() != "NONE"
            ):
                raise WorkerError(
                    "unsupported_audio",
                    f"Expected mono PCM16 WAV at one of {sorted(allowed_rates)} Hz",
                )
            samples = handle.getnframes()
            rate = handle.getframerate()
            duration = samples / rate
            if samples <= 0 or not minimum <= duration <= maximum:
                raise WorkerError(
                    "resource_limit",
                    f"WAV duration must be {minimum:g}..{maximum:g} seconds",
                )
            if len(handle.readframes(samples)) != samples * 2:
                raise WorkerError("invalid_audio", "The WAV is truncated")
    except wave.Error as exc:
        raise WorkerError("invalid_audio", f"Invalid WAV: {exc}") from exc
    return {
        "path": str(path),
        "sample_rate": rate,
        "samples": samples,
        "duration_s": duration,
    }


def normalized_script(text: str, voices: int) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise WorkerError("missing_text", "VibeVoice requires nonempty script text")
    matches = [re.fullmatch(r"Speaker\s+([1-4]):\s*(\S.*)", line, re.IGNORECASE) for line in lines]
    if not any(matches):
        if voices != 1:
            raise WorkerError("invalid_script", "Plain text requires exactly one voice reference")
        if any(re.match(r"Speaker\s+\d+\s*:", line, re.IGNORECASE) for line in lines):
            raise WorkerError("invalid_script", "Speaker IDs must be 1..4 with nonempty text")
        return "Speaker 1: " + " ".join(lines)
    if not all(matches):
        raise WorkerError("invalid_script", "Every line must be Speaker N: text")
    ids = {int(match[1]) for match in matches}
    if ids != set(range(1, voices + 1)):
        raise WorkerError("invalid_script", "Use contiguous Speaker 1..N and one reference per speaker")
    return "\n".join(f"Speaker {int(match[1])}: {match[2]}" for match in matches)


def validate_request(request: Request, *, load_only: bool = False) -> None:
    runtime_spec(request.engine)
    if type(request.max_new_tokens) is not int or not 1 <= request.max_new_tokens <= MAX_NEW_TOKENS:
        raise WorkerError("resource_limit", f"max_new_tokens must be 1..{MAX_NEW_TOKENS}")
    for value, name, limit in (
        (request.max_seconds, "max_seconds", MAX_SECONDS),
        (request.timeout_seconds, "timeout_seconds", 3_600),
    ):
        if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= limit:
            raise WorkerError("resource_limit", f"{name} must be finite, positive, and <= {limit}")
    if not math.isfinite(request.cfg_scale) or not 0 <= request.cfg_scale <= 4:
        raise WorkerError("resource_limit", "cfg_scale must be finite and 0..4")
    if type(request.inference_steps) is not int or not 1 <= request.inference_steps <= 50:
        raise WorkerError("resource_limit", "inference_steps must be 1..50")
    if type(request.seed) is not int or not 0 <= request.seed <= 2**32 - 1:
        raise WorkerError("resource_limit", "seed must be 0..2**32-1")
    for text in (request.text, request.persona):
        if text is not None and (len(text) > MAX_TEXT_CHARS or len(text.encode()) > MAX_TEXT_BYTES):
            raise WorkerError("resource_limit", "Text/persona exceeds the 8192-character limit")
    if load_only:
        return
    if request.output is None:
        raise WorkerError("missing_output", "Generation requires an output WAV")
    if request.engine == "vibevoice":
        if request.input_audio is not None or request.persona is not None:
            raise WorkerError("incompatible_capability", "VibeVoice takes a script and WAV references")
        normalized_script(request.text or "", len(request.voice_refs))
        if not 1 <= len(request.voice_refs) <= 4:
            raise WorkerError("missing_voice", "VibeVoice requires 1..4 voice references")
    else:
        if request.text is not None:
            raise WorkerError(
                "incompatible_capability",
                "PersonaPlex is speech-to-speech; provide input_audio and persona",
            )
        if request.input_audio is None or not request.persona or not request.persona.strip():
            raise WorkerError("missing_conversation_input", "PersonaPlex requires input_audio and persona")
        if len(request.voice_refs) != 1:
            raise WorkerError("missing_voice", "PersonaPlex requires one raw WAV voice reference")


def _safetensors_header(path: Path) -> dict:
    path = local_file(path)
    with path.open("rb") as handle:
        raw = handle.read(8)
        if len(raw) != 8:
            raise WorkerError("invalid_weights", f"Truncated safetensors header: {path}")
        length = struct.unpack("<Q", raw)[0]
        if not 2 <= length <= MAX_JSON_BYTES or length + 8 > path.stat().st_size:
            raise WorkerError("invalid_weights", f"Invalid safetensors header: {path}")
        header = json.loads(handle.read(length))
    if not isinstance(header, dict) or not any(key != "__metadata__" for key in header):
        raise WorkerError("invalid_weights", f"Empty safetensors checkpoint: {path}")
    return {"path": str(path), "bytes": path.stat().st_size, "tensor_count": len(header) - 1}


def _vibevoice_assets(request: Request) -> tuple[dict, list[dict]]:
    root = request.model_dir.resolve()
    config = read_json(root / "config.json")
    processor = read_json(root / "preprocessor_config.json")
    decoder = config.get("decoder_config")
    if (
        config.get("model_type") != "vibevoice"
        or not isinstance(decoder, dict)
        or decoder.get("model_type") != "qwen2"
        or config.get("torch_dtype") not in {"bfloat16", "float16", "float32"}
    ):
        raise WorkerError("unsupported_model", "Unsupported VibeVoice checkpoint configuration")
    audio = processor.get("audio_processor", {})
    if processor.get("speech_tok_compress_ratio", 3200) != 3200 or audio.get("sampling_rate") != SAMPLE_RATE:
        raise WorkerError("unsupported_model", "Only 24 kHz / 3200-compress VibeVoice is supported")
    tokenizer = (request.tokenizer or root / "tokenizer").resolve()
    for name in ("tokenizer_config.json", "tokenizer.json", "vocab.json", "merges.txt"):
        local_file(tokenizer / name, max_bytes=MAX_JSON_BYTES)
    index_path = root / "model.safetensors.index.json"
    if (root / "model.safetensors").is_file() and index_path.is_file():
        raise WorkerError("ambiguous_weights", "Keep either one checkpoint or an indexed shard set")
    if index_path.is_file():
        index = read_json(index_path)
        mapping = index.get("weight_map")
        if not isinstance(mapping, dict) or not mapping:
            raise WorkerError("invalid_weights", "VibeVoice shard index is empty")
        names = set(mapping.values())
        if any(not isinstance(name, str) or Path(name).is_absolute() for name in names):
            raise WorkerError("invalid_weights", "VibeVoice shard paths must be local filenames")
        headers = [_safetensors_header(root / name) for name in sorted(names)]
    else:
        headers = [_safetensors_header(root / "model.safetensors")]
    return {
        "config": config,
        "processor_config": processor,
        "tokenizer": str(tokenizer),
        "model_precision": config["torch_dtype"],
    }, headers


def _personaplex_assets(request: Request) -> tuple[dict, list[dict]]:
    root = request.model_dir.resolve()
    headers = [
        _safetensors_header(root / "model.safetensors"),
        _safetensors_header(root / "tokenizer-e351c8d8-checkpoint125.safetensors"),
    ]
    tokenizer = (request.tokenizer or root / "tokenizer_spm_32k_3.model").resolve()
    local_file(tokenizer, max_bytes=MAX_JSON_BYTES)
    return {"tokenizer": str(tokenizer), "model_precision": "bfloat16"}, headers


def preflight(request: Request, *, load_only: bool = False, resident: dict | None = None) -> dict:
    errors: list[dict] = []
    spec = runtime_spec(request.engine)
    report: dict = {
        "schema_version": "1.0",
        "ok": False,
        "ready": False,
        "preflight": True,
        "synthesizedSpeech": False,
        "engine": request.engine,
        "capability": spec["capability"],
        "inference_validated": False,
        "assets": {},
        "runtime": spec,
        "errors": errors,
    }

    def check(operation):
        try:
            return operation()
        except (WorkerError, OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"code": getattr(exc, "code", "invalid_asset"), "message": str(exc)})
            return None

    check(lambda: validate_request(request, load_only=load_only))
    versions = check(lambda: _runtime_versions(spec))
    spec["versions"] = versions or {}  # Installed versions, not a second copy of the manifest.
    assets_result = check(
        lambda: _vibevoice_assets(request)
        if request.engine == "vibevoice"
        else _personaplex_assets(request)
    )
    if assets_result is not None:
        assets, weights = assets_result
        report["assets"] = {**assets, "weights": weights}
        report["model_dir"] = str(request.model_dir.resolve())
    else:
        weights = []
    if not load_only:
        for path in request.voice_refs:
            info = check(
                lambda path=path: inspect_wav(
                    path,
                    maximum=MAX_REFERENCE_SECONDS,
                    minimum=0.5 if request.engine == "personaplex" else 0.0,
                    allowed_rates=(16_000, 22_050, 24_000, 44_100, 48_000),
                )
            )
            if info:
                report["assets"].setdefault("voices", []).append(info)
        if request.input_audio is not None:
            info = check(
                lambda: inspect_wav(
                    request.input_audio,
                    maximum=min(MAX_SECONDS, request.max_seconds),
                )
            )
            if info:
                report["assets"]["input_audio"] = info
                if request.engine == "personaplex" and math.ceil(info["samples"] / 1920) > request.max_new_tokens:
                    errors.append(
                        {
                            "code": "resource_limit",
                            "message": "PersonaPlex input needs more stream steps than max_new_tokens",
                        }
                    )
    execution = None
    if versions is not None:
        execution = resident["execution"] if resident is not None else check(lambda: resolve(preferences(request)))
    if execution is not None and execution["backend"] == "cuda":
        actual = execution.get("runtime", {}).get("cuda")
        if actual != spec["cuda_build"]:
            errors.append({
                "code": "runtime_mismatch",
                "message": f"Worker requires a CUDA {spec['cuda_build']} torch build; found {actual!r}",
            })
            execution = None
    if execution is not None:
        report["execution"] = execution
        report["device"] = execution["torch_device"]
        report["dtype"] = execution["precision"]
    required = (26 if request.engine == "vibevoice" else 18) * GIB
    memory = (
        {
            **memory_snapshot(),
            "required_bytes": 6 * GIB,
            "resident": True,
            "safe_budget_bytes": 6 * GIB,
            "sufficient": True,
        }
        if resident is not None
        else check(lambda: require_host_memory(required))
    )
    report["memory"] = memory
    if request.output is not None:
        output = request.output
        if output.suffix.lower() != ".wav":
            errors.append({"code": "invalid_output", "message": "Output must end in .wav"})
        if output.exists():
            errors.append({"code": "output_exists", "message": f"Refusing to overwrite {output}"})
        if not output.parent.is_dir():
            errors.append({"code": "missing_output_directory", "message": f"Output directory does not exist: {output.parent}"})
    report["limits"] = {
        "max_new_tokens": request.max_new_tokens,
        "max_seconds": request.max_seconds,
        "timeout_seconds": request.timeout_seconds,
    }
    report["ready"] = report["ok"] = not errors
    return report

