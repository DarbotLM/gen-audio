"""Stdlib HTTP surface for genaid-audio liveness, readiness, and AgentYankers.

Importing this module does not bind a socket. ``serve_loopback`` is the
opt-in listener. ``/health`` is liveness and stays 200 when the model is
unloaded. ``/ready`` is the lazy-load readiness probe.
``POST /genaid-audio/agentyankers/episode`` dry-runs or renders a rundown.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gen_audio.serve import (
    AGENTYANKERS_EPISODE_PATH,
    AGENTYANKERS_FORMAT_PATH,
    DEFAULT_PORT,
    ROUTE_PREFIX,
    health_payload,
    ready_payload,
)

_FORMAT_ROUTE = f"{ROUTE_PREFIX}{AGENTYANKERS_FORMAT_PATH}"
_EPISODE_ROUTE = f"{ROUTE_PREFIX}{AGENTYANKERS_EPISODE_PATH}"


def dispatch(
    method: str,
    path: str,
    *,
    model_loaded: bool = False,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    """Return ``(status, json)`` for one request. Unknown routes are 404."""
    route = path.split("?", 1)[0].rstrip("/") or "/"
    if method == "GET" and route == f"{ROUTE_PREFIX}/health":
        payload: dict[str, object] = dict(health_payload())
        payload["pid"] = os.getpid()
        payload["model_loaded"] = model_loaded
        return 200, payload
    if method == "GET" and route == f"{ROUTE_PREFIX}/ready":
        return 200, dict(ready_payload(model_loaded=model_loaded))
    if method == "GET" and route in {_FORMAT_ROUTE, _EPISODE_ROUTE}:
        from gen_audio.agentyankers.serve_route import format_descriptor

        return 200, format_descriptor()
    if method == "POST" and route == _EPISODE_ROUTE:
        from gen_audio.agentyankers.serve_route import handle_episode

        if not isinstance(body, dict):
            return 400, {"error": "invalid_body", "message": "episode body must be a JSON object"}
        return handle_episode(body)
    if method != "GET":
        return 405, {"error": "method not allowed"}
    return 404, {"error": "not found"}


def serve_loopback(host: str = "127.0.0.1", port: int | None = None) -> ThreadingHTTPServer:
    """Bind a loopback listener. The caller starts ``serve_forever``."""
    if port is None:
        raw = os.environ.get("GEN_AUDIO_NODE_PORT", str(DEFAULT_PORT)).strip()
        try:
            port = int(raw)
        except ValueError as exc:
            raise ValueError(f"GEN_AUDIO_NODE_PORT must be an integer, got {raw!r}") from exc
        if port < 1 or port > 65535:
            raise ValueError(f"GEN_AUDIO_NODE_PORT must be between 1 and 65535, got {port}")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status, payload = dispatch("GET", self.path)
            self._send(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            from gen_audio.agentyankers.serve_route import BodyError, parse_request_body

            try:
                raw = self._read_body()
                payload = parse_request_body(
                    raw,
                    self.headers.get("Content-Type"),
                    self.headers.get("Content-Length"),
                )
            except BodyError as exc:
                self._send(exc.status, exc.payload)
                return
            status, response = dispatch("POST", self.path, body=payload)
            self._send(status, response)

        def _read_body(self) -> bytes:
            from gen_audio.agentyankers.serve_route import MAX_BODY_BYTES, BodyError

            length = self.headers.get("Content-Length")
            if length is None:
                raise BodyError(411, {"error": "length_required", "message": "Content-Length is required"})
            try:
                size = int(length)
            except ValueError as exc:
                raise BodyError(
                    400,
                    {"error": "invalid_body", "message": "Content-Length must be an integer"},
                ) from exc
            if size < 0 or size > MAX_BODY_BYTES:
                raise BodyError(
                    413,
                    {
                        "error": "body_too_large",
                        "message": f"body must be at most {MAX_BODY_BYTES} bytes",
                    },
                )
            return self.rfile.read(size)

        def _send(self, status: int, payload: dict[str, object]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
