"""Small loopback clients for the isolated 7B worker services."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_URL_ENV = {
    "vibevoice": "GEN_AUDIO_VIBEVOICE_WORKER_URL",
    "personaplex": "GEN_AUDIO_PERSONAPLEX_WORKER_URL",
}
_MAX_JSON = 4 * 1024 * 1024


def worker_url(engine: str) -> str | None:
    if engine not in _URL_ENV:
        raise ValueError(f"unknown 7B worker engine: {engine}")
    value = os.environ.get(_URL_ENV[engine], "").strip().rstrip("/")
    return value or None


def worker_request(
    engine: str,
    path: str,
    *,
    payload: dict | None = None,
    timeout: float = 10,
) -> dict:
    base = worker_url(engine)
    if base is None:
        raise ValueError(f"{_URL_ENV[engine]} is unset")
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"}
        if body is not None
        else {"Accept": "application/json"},
        method="GET" if body is None else "POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_JSON + 1)
    except HTTPError as exc:
        raw = exc.read(_MAX_JSON + 1)
    except URLError as exc:
        raise ValueError(f"{engine} worker is unavailable: {exc.reason}") from exc
    if len(raw) > _MAX_JSON:
        raise ValueError(f"{engine} worker response exceeds {_MAX_JSON} bytes")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{engine} worker returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(  # noqa: TRY004
            f"{engine} worker returned a non-object response"
        )
    return value


def worker_status(engine: str) -> dict:
    return worker_request(engine, "/v1/status")


def require_worker(engine: str) -> dict:
    status = worker_status(engine)
    if status.get("ok") is not True or status.get("loaded") is not True:
        errors = status.get("errors", [])
        detail = errors[0].get("message") if errors and isinstance(errors[0], dict) else status.get("phase")
        raise ValueError(f"{engine} worker is not ready: {detail or 'model is unloaded'}")
    return status


def load_worker(engine: str, *, timeout: float = 1_800) -> dict:
    result = worker_request(engine, "/v1/load", payload={}, timeout=timeout)
    if result.get("ok") is False or result.get("loaded") is not True:
        errors = result.get("errors", [])
        detail = errors[0].get("message") if errors and isinstance(errors[0], dict) else result.get("phase")
        raise ValueError(f"{engine} worker failed to load: {detail or 'unknown load failure'}")
    return result


def preflight_worker(engine: str) -> dict:
    result = worker_request(engine, "/v1/preflight", timeout=30)
    if result.get("ready") is not True:
        raise ValueError(f"{engine} worker preflight is not ready")
    return result


def unload_worker(engine: str) -> dict:
    return worker_request(engine, "/v1/unload", payload={}, timeout=30)


def generate_worker(
    engine: str,
    body: dict,
    *,
    local_output: Path | None = None,
    timeout: float = 1_200,
) -> dict:
    receipt = worker_request(engine, "/v1/generate", payload=body, timeout=timeout)
    if receipt.get("ok") is not True or receipt.get("synthesizedSpeech") is not True:
        errors = receipt.get("errors", [])
        detail = errors[0].get("message") if errors and isinstance(errors[0], dict) else "unknown generation failure"
        raise ValueError(f"{engine} worker failed to generate: {detail}")
    if local_output is not None:
        source = Path(receipt["output"])
        if not source.is_file():
            raise ValueError(f"{engine} worker receipt names a missing WAV")
        local_output.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != local_output.resolve():
            shutil.copy2(source, local_output)
    return receipt

