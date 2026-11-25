"""Standard-library-only contracts shared by the 7B workers and clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GIB = 1024**3
SAMPLE_RATE = 24_000
MAX_JSON_BYTES = 16 * 1024**2
MAX_TEXT_BYTES = 32 * 1024
MAX_TEXT_CHARS = 8_192
MAX_REFERENCE_SECONDS = 30.0
MAX_SECONDS = 60.0
MAX_NEW_TOKENS = 1_024
MAX_INPUT_TOKENS = 4_096

VIBEVOICE_REVISION = "952326ddb264062466a888cf32a5b2f4e803e16e"
PERSONAPLEX_REVISION = "3428dfd95309a7f3c84fd93259ded0f810d1ff91"


class WorkerError(ValueError):
    """A user/actionable worker failure with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Request:
    engine: str
    model_dir: Path
    output: Path | None = None
    text: str | None = None
    input_audio: Path | None = None
    persona: str | None = None
    voice_refs: tuple[Path, ...] = ()
    tokenizer: Path | None = None
    device_index: int = 0
    max_new_tokens: int = 256
    max_seconds: float = 30.0
    timeout_seconds: float = 600.0
    seed: int = 0
    cfg_scale: float = 1.3
    inference_steps: int = 10
    execution: dict[str, Any] | None = None
    agent_names: tuple[str, ...] = ()


@dataclass
class WorkerStatus:
    engine: str
    phase: str = "unloaded"
    loaded: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def runtime_spec(engine: str) -> dict[str, Any]:
    if engine == "vibevoice":
        return {
            "engine": engine,
            "distribution": "vibevoice",
            "repository": "https://github.com/vibevoice-community/VibeVoice",
            "revision": VIBEVOICE_REVISION,
            "requirements": "runtimes/requirements-vibevoice-hybrid.txt",
            "capability": "script_tts",
            "versions": {
                "vibevoice": "0.1.0",
                "torch": "2.6.0+cu124",
                "transformers": "4.51.3",
                "accelerate": "1.6.0",
                "safetensors": "0.5.3",
                "numpy": "2.1.3",
            },
        }
    if engine == "personaplex":
        return {
            "engine": engine,
            "distribution": "moshi-personaplex",
            "repository": "https://github.com/NVIDIA/personaplex",
            "revision": PERSONAPLEX_REVISION,
            "requirements": "runtimes/requirements-personaplex-hybrid.txt",
            "capability": "speech_to_speech",
            "versions": {
                "moshi-personaplex": "0.1.0",
                "torch": "2.4.1+cu121",
                "accelerate": "1.6.0",
                "numpy": "1.26.4",
                "safetensors": "0.4.5",
                "sentencepiece": "0.2.0",
            },
        }
    raise WorkerError("unsupported_engine", f"Unknown engine: {engine}")

