"""Resolve and account for bounded CUDA + host-RAM hybrid execution."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from .contracts import GIB, WorkerError


@dataclass(frozen=True)
class Execution:
    backend: str = "cuda"
    device: str = "gpu"
    device_index: int = 0
    precision: str = "auto"
    attention: str = "auto"
    offload: str = "cpu"
    gpu_memory_gib: float = 5.75
    cpu_memory_gib: float = 48.0

    @classmethod
    def parse(cls, value) -> Execution:
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise WorkerError(
                "invalid_execution",
                "Execution contains unknown options; use backend, device, "
                "device_index, precision, attention, offload, gpu_memory_gib, "
                "and cpu_memory_gib",
            )
        try:
            result = cls(**value)
        except TypeError as exc:
            raise WorkerError("invalid_execution", str(exc)) from exc
        if result.backend not in {"cuda", "cpu"}:
            raise WorkerError("unsupported_backend", "Only cuda and cpu backends are supported")
        if result.device not in {"gpu", "cpu"}:
            raise WorkerError("invalid_execution", "device must be gpu or cpu")
        if (result.backend == "cpu") != (result.device == "cpu"):
            raise WorkerError("invalid_execution", "backend and device disagree")
        if result.precision not in {"auto", "float32", "float16", "bfloat16"}:
            raise WorkerError("invalid_execution", "Unsupported precision")
        if result.attention not in {"auto", "math", "efficient"}:
            raise WorkerError("invalid_execution", "Unsupported attention policy")
        if result.offload not in {"cpu", "none"}:
            raise WorkerError("invalid_execution", "offload must be cpu or none")
        if type(result.device_index) is not int or not 0 <= result.device_index <= 15:
            raise WorkerError("invalid_execution", "device_index must be 0..15")
        for candidate, name, maximum in (
            (result.gpu_memory_gib, "gpu_memory_gib", 7.25),
            (result.cpu_memory_gib, "cpu_memory_gib", 60.0),
        ):
            if (
                isinstance(candidate, bool)
                or not isinstance(candidate, (int, float))
                or not 0.5 <= candidate <= maximum
            ):
                raise WorkerError("resource_limit", f"{name} is outside its safe range")
        if result.backend == "cpu" and result.device_index:
            raise WorkerError("invalid_execution", "CPU device_index must be zero")
        return result


def preferences(request) -> Execution:
    options = Execution.parse(request.execution)
    if options == Execution() and request.device_index:
        options = Execution(device_index=request.device_index)
    return options


def resolve(options: Execution) -> dict:
    requested = asdict(options)
    if options.backend == "cpu":
        precision = "float32" if options.precision == "auto" else options.precision
        if precision == "float16":
            raise WorkerError("unsupported_precision", "CPU workers require float32 or bfloat16")
        return {
            "schema_version": "1.0",
            "mode": "cpu",
            "backend": "cpu",
            "device": "cpu",
            "device_index": 0,
            "torch_device": "cpu",
            "precision": precision,
            "requested": requested,
            "attention": options.attention,
            "offload": "none",
            "gpu_memory_bytes": 0,
            "cpu_memory_bytes": int(options.cpu_memory_gib * GIB),
        }

    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    try:
        import torch
    except ImportError as exc:
        raise WorkerError("missing_dependency", "The isolated CUDA runtime does not provide PyTorch") from exc
    if not torch.cuda.is_available():
        raise WorkerError("accelerator_unavailable", "CUDA is unavailable in the isolated worker")
    if options.device_index >= torch.cuda.device_count():
        raise WorkerError("accelerator_unavailable", "Requested CUDA device is unavailable")
    with torch.cuda.device(options.device_index):
        properties = torch.cuda.get_device_properties(options.device_index)
        available, total = torch.cuda.mem_get_info()
    precision = "float16" if options.precision == "auto" else options.precision
    capability = properties.major
    if precision == "bfloat16" and capability < 8:
        raise WorkerError(
            "unsupported_precision",
            f"{properties.name} reports compute capability {properties.major}.{properties.minor}; "
            "use float16 for Turing-class T1000 hardware",
        )
    if precision == "float32" and options.offload == "none":
        raise WorkerError("resource_limit", "A 7B FP32 whole-model GPU load cannot fit an 8 GiB device")
    budget = min(int(options.gpu_memory_gib * GIB), int(total * 0.90), max(0, available - GIB // 2))
    requested_bytes = int(options.gpu_memory_gib * GIB)
    if budget < requested_bytes:
        raise WorkerError(
            "insufficient_gpu_memory",
            f"Requested {options.gpu_memory_gib:.2f} GiB on CUDA {options.device_index}; "
            f"only {(budget / GIB):.2f} GiB is safe right now",
        )
    return {
        "schema_version": "1.0",
        "mode": "hybrid" if options.offload == "cpu" else "device",
        "backend": "cuda",
        "device": "gpu",
        "device_index": options.device_index,
        "torch_device": f"cuda:{options.device_index}",
        "precision": precision,
        "requested": requested,
        "attention": options.attention,
        "offload": options.offload,
        "gpu_memory_bytes": requested_bytes,
        "cpu_memory_bytes": int(options.cpu_memory_gib * GIB),
        "hardware": {
            "name": properties.name,
            "architecture": getattr(properties, "gcnArchName", None),
            "total_bytes": total,
            "available_bytes": available,
            "compute_capability": f"{properties.major}.{properties.minor}",
        },
        "runtime": {
            "torch": str(torch.__version__),
            "cuda": torch.version.cuda,
        },
    }


def max_memory(execution: dict) -> dict[int | str, int]:
    limits = {"cpu": execution["cpu_memory_bytes"]}
    if execution["backend"] == "cuda":
        limits[execution["device_index"]] = execution["gpu_memory_bytes"]
    return limits


def execution_json(execution: dict) -> str:
    return json.dumps(execution["requested"], sort_keys=True, separators=(",", ":"))

