"""Loopback-only resident HTTP service for the isolated 7B model workers."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .contracts import SAMPLE_RATE, Request, WorkerError, WorkerStatus
from .execution import preferences
from .preflight import preflight
from .runtime import execute, offline_runtime

_MAX_BODY = 64 * 1024


def _bounded_json(value) -> dict:
    if not isinstance(value, dict):
        raise WorkerError("invalid_request", "Request body must be a JSON object")
    return value


def _path_under(root: Path, raw: str) -> Path:
    root = root.resolve()
    candidate = Path(raw).expanduser()
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if candidate != root and root not in candidate.parents:
        raise WorkerError("invalid_path", f"Path escapes the configured root: {raw}")
    return candidate


class WorkerService:
    def __init__(
        self,
        *,
        engine: str,
        model_dir: Path,
        tokenizer: Path,
        execution: dict,
        output_root: Path,
        voice_root: Path,
    ) -> None:
        self.engine = engine
        self.model_dir = model_dir.resolve()
        self.tokenizer = tokenizer.resolve()
        self.execution = execution
        self.output_root = output_root.resolve()
        self.voice_root = voice_root.resolve()
        self.status = WorkerStatus(engine=engine)
        self.runtime: dict | None = None
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{engine}-7b")
        self.started_at = time.time()
        self.active_stream = None

    def _base_request(self) -> Request:
        return Request(
            engine=self.engine,
            model_dir=self.model_dir,
            tokenizer=self.tokenizer,
            execution=self.execution,
        )

    def _load(self) -> dict:
        with self.lock:
            request = self._base_request()
            report = preflight(request, load_only=True)
            if not report["ready"]:
                self.status.phase = "unavailable"
                return report
            self.status.phase = "loading"
            deadline = time.monotonic() + 1_800
            started = time.perf_counter()
            try:
                with offline_runtime():
                    if self.engine == "vibevoice":
                        from .vibevoice import load
                    else:
                        from .personaplex import load
                    runtime = load(request, report, deadline)
            except Exception:
                self.status.phase = "unavailable"
                raise
            self.runtime = runtime
            self.status.loaded = True
            self.status.phase = "ready"
            device_map = runtime.get("device_map", {})
            self.status.metadata = {
                "pid": os.getpid(),
                "engine": self.engine,
                "loaded": True,
                "phase": "ready",
                "model_dir": str(self.model_dir),
                "tokenizer": str(self.tokenizer),
                "execution": runtime["execution"],
                "load_seconds": time.perf_counter() - started,
                "load_memory": runtime["load_memory"],
                "placement_strategy": runtime.get("placement_strategy"),
                "cpu_threads": runtime.get("cpu_threads"),
                "gpu_layers": runtime.get("gpu_layers"),
                "device_map": {
                    "entries": len(device_map),
                    "gpu": sum(value in (0, 1, runtime["execution"]["device_index"]) for value in device_map.values()),
                    "cpu": sum(value == "cpu" for value in device_map.values()),
                },
            }
            return dict(self.status.metadata)

    async def load(self) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._load)

    def _preflight(self) -> dict:
        with self.lock:
            return preflight(self._base_request(), load_only=True)

    async def preflight(self) -> dict:
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, self._preflight
        )

    def _unload(self) -> dict:
        with self.lock:
            self._close_realtime()
            self.runtime = None
            self.status = WorkerStatus(engine=self.engine, phase="unloaded")
            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass
            return {"engine": self.engine, "loaded": False, "phase": "unloaded"}

    async def unload(self) -> dict:
        return await asyncio.get_running_loop().run_in_executor(self.executor, self._unload)

    def _resolve_voice(self, raw: str) -> Path:
        value = Path(raw)
        if value.is_absolute():
            candidate = value.resolve()
            roots = (self.voice_root, self.output_root)
            if not any(candidate == root or root in candidate.parents for root in roots):
                raise WorkerError("invalid_voice_path", "Voice reference escapes configured roots")
        else:
            candidate = (self.voice_root / value).resolve()
            if not candidate.is_file():
                try:
                    from gen_audio.genaid_voices import resolve_sample

                    candidate = resolve_sample(value.stem, root=self.voice_root)
                except ValueError:
                    pass
            if not candidate.is_file():
                candidates = []
                name = value.name if value.suffix else f"{value.name}.wav"
                for root in (
                    self.voice_root / "samples" / "vibevoice",
                    self.voice_root / "samples",
                    self.voice_root / "voices",
                    self.voice_root,
                ):
                    if root.is_dir():
                        candidates.extend(path for path in root.rglob(name) if path.is_file())
                if not candidates:
                    raise WorkerError("missing_voice", f"Voice reference not found in GENAID library: {raw}")
                candidate = min(candidates).resolve()
            roots = (self.voice_root,)
            if not any(candidate == root or root in candidate.parents for root in roots):
                raise WorkerError("invalid_voice_path", "Voice reference escapes GENAID library")
        if candidate.suffix.lower() != ".wav":
            raise WorkerError("unsupported_voice_format", "Only raw WAV references are accepted; .pt is never unpickled")
        return candidate

    def _request(self, body: dict) -> Request:
        output_raw = body.get("output")
        if not output_raw:
            output_raw = (
                f"generated/{self.engine}/{uuid4().hex}.wav"
            )
        output = _path_under(self.output_root, output_raw)
        output.parent.mkdir(parents=True, exist_ok=True)
        refs = tuple(self._resolve_voice(str(value)) for value in body.get("voiceRefs", []))
        input_raw = body.get("inputAudio")
        input_audio = None
        if input_raw:
            input_audio = Path(input_raw)
            if not input_audio.is_absolute():
                input_audio = (self.output_root / input_audio).resolve()
            if not any(
                input_audio == root or root in input_audio.parents
                for root in (self.voice_root, self.output_root)
            ):
                raise WorkerError("invalid_audio_path", "Input audio escapes configured roots")
        return Request(
            engine=self.engine,
            model_dir=self.model_dir,
            tokenizer=self.tokenizer,
            output=output,
            text=body.get("scriptText") if self.engine == "vibevoice" else None,
            input_audio=input_audio,
            persona=body.get("persona") if self.engine == "personaplex" else None,
            voice_refs=refs,
            max_new_tokens=int(body.get("maxNewTokens", 256)),
            max_seconds=float(body.get("maxSeconds", 30)),
            timeout_seconds=float(body.get("timeoutSeconds", 900)),
            seed=int(body.get("seed", 0)),
            cfg_scale=float(body.get("cfgScale", 1.3)),
            inference_steps=int(body.get("inferenceSteps", 10)),
            execution=self.execution,
            agent_names=tuple(body.get("agentNames", [])),
            device_index=int(self.execution.get("device_index", 0)),
        )

    def _generate(self, body: dict) -> dict:
        with self.lock:
            if self.runtime is None:
                raise WorkerError("model_not_loaded", f"{self.engine} is not loaded")
            request = self._request(body)
            return execute(request, resident=self.runtime)

    async def generate(self, body: dict) -> dict:
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, self._generate, _bounded_json(body)
        )

    def _start_realtime(self, query: dict):
        from .personaplex import PersonaPlexRealtime

        with self.lock:
            if self.engine != "personaplex" or self.runtime is None:
                raise WorkerError("model_not_loaded", "PersonaPlex is not loaded")
            if self.active_stream is not None:
                raise WorkerError("worker_busy", "PersonaPlex realtime session is already active")
            raw_voice = str(query.get("voice_prompt", "")).strip()
            voice_id = raw_voice.removesuffix(".pt")
            if not voice_id:
                raise WorkerError("missing_voice", "DLM Speech did not name a voice prompt")
            voice_ref = self._resolve_voice(voice_id)
            stream = PersonaPlexRealtime(
                self.runtime,
                voice_ref=voice_ref,
                persona=str(query.get("text_prompt", "")),
                seed=int(query.get("seed", 0)),
                audio_temperature=float(query.get("audio_temperature", 0.8)),
                text_temperature=float(query.get("text_temperature", 0.7)),
                audio_top_k=int(query.get("audio_top_k", 250)),
                text_top_k=int(query.get("text_top_k", 25)),
            )
            stream.start()
            self.active_stream = stream
            return stream

    async def start_realtime(self, query: dict):
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, self._start_realtime, dict(query)
        )

    def _feed_realtime(self, audio):
        stream = self.active_stream
        if stream is None:
            raise WorkerError("stream_closed", "PersonaPlex stream is closed")
        return stream.feed(audio)

    async def feed_realtime(self, audio):
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, self._feed_realtime, audio
        )

    def _close_realtime(self):
        stream = self.active_stream
        self.active_stream = None
        if stream is not None:
            stream.close()

    async def close_realtime(self):
        await asyncio.get_running_loop().run_in_executor(self.executor, self._close_realtime)

    def metadata(self) -> dict:
        if not self.status.loaded or self.runtime is None:
            return {
                "schema_version": "1.0",
                "engine": self.engine,
                "pid": os.getpid(),
                "loaded": False,
                "phase": self.status.phase,
                "uptime_seconds": time.time() - self.started_at,
            }
        return {
            **self.status.metadata,
            "uptime_seconds": time.time() - self.started_at,
        }


async def _body(request) -> dict:
    length = request.content_length or 0
    if length > _MAX_BODY:
        raise WorkerError("resource_limit", "JSON request exceeds 64 KiB")
    raw = await request.read()
    if len(raw) > _MAX_BODY:
        raise WorkerError("resource_limit", "JSON request exceeds 64 KiB")
    try:
        return _bounded_json(json.loads(raw or b"{}"))
    except json.JSONDecodeError as exc:
        raise WorkerError("invalid_request", f"Invalid JSON: {exc.msg}") from exc


def _reply_error(error: WorkerError, status: int = 422):
    from aiohttp import web

    return web.json_response(
        {
            "ok": False,
            "synthesizedSpeech": False,
            "errors": [{"code": error.code, "message": str(error)}],
        },
        status=status,
    )


def application(service: WorkerService):
    from aiohttp import web

    async def status(_request):
        return web.json_response({"ok": True, **service.metadata()})

    async def root(_request):
        metadata = service.metadata()
        return web.json_response(metadata, status=200 if metadata.get("loaded") else 503)

    async def load(_request):
        receipt = await service.load()
        return web.json_response(receipt, status=200 if receipt.get("loaded") else 422)

    async def run_preflight(_request):
        receipt = await service.preflight()
        return web.json_response(receipt, status=200 if receipt.get("ready") else 422)

    async def unload(_request):
        return web.json_response(await service.unload())

    async def generate(request):
        try:
            receipt = await service.generate(await _body(request))
            return web.json_response(receipt, status=200 if receipt.get("ok") else 422)
        except WorkerError as exc:
            return _reply_error(exc)
        except (OSError, ValueError, RuntimeError) as exc:
            return _reply_error(WorkerError("generation_failed", f"{type(exc).__name__}: {str(exc)[:1000]}"), 500)

    async def chat(request):
        import numpy as np
        import sphn
        from aiohttp import WSMsgType

        socket = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 1024)
        await socket.prepare(request)
        try:
            await service.start_realtime(request.query)
            await socket.send_bytes(b"\x00")
            reader = sphn.OpusStreamReader(SAMPLE_RATE)
            writer = sphn.OpusStreamWriter(SAMPLE_RATE)
            async for message in socket:
                if message.type != WSMsgType.BINARY or not message.data:
                    continue
                kind, payload = message.data[0], message.data[1:]
                if kind != 1:
                    continue
                pcm = reader.append_bytes(payload)
                if pcm is None or not np.size(pcm):
                    continue
                outputs, text, _elapsed = await service.feed_realtime(
                    np.asarray(pcm, dtype=np.float32).reshape(-1)
                )
                for output in outputs:
                    opus = writer.append_pcm(output)
                    if opus:
                        await socket.send_bytes(b"\x01" + opus)
                if text:
                    await socket.send_bytes(b"\x02" + text.encode("utf-8"))
        except WorkerError as exc:
            if not socket.closed:
                await socket.send_json(
                    {"version": 1, "type": "error", "error": {"code": exc.code, "message": str(exc)}}
                )
        finally:
            await service.close_realtime()
            if not socket.closed:
                await socket.close()
        return socket

    app = web.Application()
    app.add_routes(
        [
            web.get("/", root),
            web.get("/status", status),
            web.get("/v1/status", status),
            web.get("/v1/preflight", run_preflight),
            web.post("/load", load),
            web.post("/v1/load", load),
            web.post("/unload", unload),
            web.post("/v1/unload", unload),
            web.post("/generate", generate),
            web.post("/v1/generate", generate),
            web.get("/api/chat", chat),
        ]
    )
    return app


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--engine", choices=("vibevoice", "personaplex"), required=True)
    result.add_argument("--model-dir", type=Path, required=True)
    result.add_argument("--tokenizer", type=Path, required=True)
    result.add_argument("--output-root", type=Path, required=True)
    result.add_argument("--voice-root", type=Path, required=True)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, required=True)
    result.add_argument("--device-index", type=int, required=True)
    result.add_argument("--backend", choices=("cuda", "cpu"), default="cuda")
    result.add_argument("--gpu-memory-gib", type=float, default=6.5)
    result.add_argument("--cpu-memory-gib", type=float, default=48)
    result.add_argument("--preload", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    from aiohttp import web

    args = parser().parse_args(argv)
    if args.host != "127.0.0.1":
        raise WorkerError("unsafe_bind", "The model worker may bind only to 127.0.0.1")
    if args.backend == "cuda":
        device = "gpu"
        precision = "float16"
    else:
        device = "cpu"
        precision = "bfloat16"
    execution = asdict(preferences(Request(
        engine=args.engine,
        model_dir=args.model_dir,
        execution={
            "backend": args.backend,
            "device": device,
            "device_index": args.device_index,
            "precision": precision,
            "attention": "math",
            "offload": "cpu" if args.backend == "cuda" else "none",
            "gpu_memory_gib": args.gpu_memory_gib,
            "cpu_memory_gib": args.cpu_memory_gib,
        },
    )))
    service = WorkerService(
        engine=args.engine,
        model_dir=args.model_dir,
        tokenizer=args.tokenizer,
        execution=execution,
        output_root=args.output_root,
        voice_root=args.voice_root,
    )
    app = application(service)

    async def cleanup(_app):
        await service.unload()
        service.executor.shutdown(wait=True)

    app.on_cleanup.append(cleanup)
    if args.preload:
        async def preload(_app):
            await service.load()
        app.on_startup.append(preload)
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkerError as exc:
        print(json.dumps({"ok": False, "errors": [{"code": exc.code, "message": str(exc)}]}), file=sys.stderr)
        raise SystemExit(2) from exc

