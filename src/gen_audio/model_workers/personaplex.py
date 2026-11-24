"""PersonaPlex 7B speech-to-speech adapter with hybrid Moshi placement."""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import time
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from types import MethodType

import psutil

from .contracts import (
    GIB,
    MAX_INPUT_TOKENS,
    MAX_REFERENCE_SECONDS,
    PERSONAPLEX_REVISION,
    SAMPLE_RATE,
    Request,
    WorkerError,
)
from .execution import max_memory
from .runtime import check_deadline, checked_audio, read_pcm


def _voice_cache_path(voice_ref: Path, output: Path | None) -> Path:
    digest = hashlib.sha256(voice_ref.read_bytes()).hexdigest()[:20]
    raw = os.environ.get("GEN_AUDIO_PERSONAPLEX_CACHE_DIR", "").strip()
    root = (
        Path(raw)
        if raw
        else (
            output.parent / ".personaplex-cache"
            if output is not None
            else Path(tempfile.gettempdir()) / "gen-audio-personaplex-cache"
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.voice.safetensors"


def _prime_voice_prompt(generator, mimi, voice_audio, cache_path: Path) -> bool:
    import torch
    from safetensors import safe_open
    from safetensors.torch import save_file

    if cache_path.is_file():
        if cache_path.stat().st_size > 128 * 1024**2:
            raise WorkerError("invalid_voice_cache", "Raw voice cache exceeds 128 MiB")
        with safe_open(str(cache_path), framework="pt", device="cpu") as cache:
            keys = set(cache.keys())
            if keys != {"embeddings", "cache"}:
                raise WorkerError("invalid_voice_cache", f"Invalid raw voice cache: {cache_path}")
            metadata = cache.metadata() or {}
            if (
                metadata.get("schema_version") != "1"
                or metadata.get("runtime_revision") != PERSONAPLEX_REVISION
            ):
                raise WorkerError("invalid_voice_cache", "Raw voice cache revision is incompatible")
            generator.voice_prompt_audio = None
            embeddings = cache.get_tensor("embeddings")
            state_cache = cache.get_tensor("cache")
            if (
                embeddings.ndim != 4
                or embeddings.shape[0] > 450
                or embeddings.shape[-1] != 4096
                or state_cache.ndim != 3
                or state_cache.shape[0] != 1
                or state_cache.shape[1] != 17
            ):
                raise WorkerError("invalid_voice_cache", "Raw voice cache tensor shapes are invalid")
            generator.voice_prompt_embeddings = embeddings
            generator.voice_prompt_cache = state_cache
        generator.step_system_prompts(mimi)
        return True

    generator.voice_prompt_audio = voice_audio[None, :]
    generator.voice_prompt_cache = None
    generator.voice_prompt_embeddings = None
    embeddings = []
    for frame in generator._encode_voice_prompt_frames(mimi):
        output = generator.step(
            moshi_tokens=frame,
            text_token=generator.zero_text_code,
            input_tokens=generator._encode_sine_frame(),
            return_embeddings=True,
        )
        if output is not None:
            _tokens, embedding = output
            embeddings.append(embedding.detach().to("cpu"))
    if not embeddings:
        raise WorkerError("invalid_voice", "Raw voice reference produced no prompt frames")
    staged = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.partial")
    values = {
        "embeddings": torch.stack(embeddings),
        "cache": generator._streaming_state.cache.detach().to("cpu"),
    }
    try:
        save_file(
            values,
            str(staged),
            metadata={
                "schema_version": "1",
                "runtime_revision": PERSONAPLEX_REVISION,
            },
        )
        os.replace(staged, cache_path)
    finally:
        staged.unlink(missing_ok=True)
    generator._step_audio_silence()
    generator._step_text_prompt()
    generator._step_audio_silence()
    return False


def load(request: Request, report: dict, deadline: float) -> dict:
    import sentencepiece
    import torch
    from accelerate.big_modeling import load_checkpoint_and_dispatch
    from moshi.models import loaders

    torch.manual_seed(request.seed)
    configured_threads = int(os.environ.get("GEN_AUDIO_PERSONAPLEX_CPU_THREADS", "24"))
    if not 1 <= configured_threads <= 32:
        raise WorkerError("invalid_execution", "GEN_AUDIO_PERSONAPLEX_CPU_THREADS must be 1..32")
    torch.set_num_threads(min(configured_threads, psutil.cpu_count(logical=True) or configured_threads))
    execution = report["execution"]
    root = request.model_dir.resolve()
    tokenizer = sentencepiece.SentencePieceProcessor(report["assets"]["tokenizer"])
    # PersonaPlex is checkpointed in BF16. Loading in that dtype avoids a
    # second 16.7 GB conversion pass on an 8 GiB device.
    dtype = torch.bfloat16
    strategy = os.environ.get("GEN_AUDIO_PERSONAPLEX_HYBRID_STRATEGY", "cpu_compute")
    if strategy not in {"cpu_compute", "streaming_offload"}:
        raise WorkerError("invalid_execution", "Unknown PersonaPlex hybrid strategy")
    lm = loaders.get_moshi_lm(
        None,
        copy_missing_weights=False,
        device="cpu",
        dtype=dtype,
        cpu_offload=False,
    )
    check_deadline(deadline)
    memory = max_memory(execution)
    # Reserve part of the explicit GPU budget for Mimi's FP32 codec and stream state.
    if execution["mode"] == "hybrid":
        memory[execution["device_index"]] -= GIB
    offload = Path(
        os.environ.get(
            "GEN_AUDIO_PERSONAPLEX_OFFLOAD_DIR",
            str(Path(tempfile.gettempdir()) / "gen-audio-personaplex-offload"),
        )
    )
    offload.mkdir(parents=True, exist_ok=True)
    if execution["mode"] == "hybrid" and strategy == "cpu_compute":
        from safetensors import safe_open

        expected = dict(lm.state_dict())
        with safe_open(
            str(root / "model.safetensors"),
            framework="pt",
            device="cpu",
        ) as checkpoint:
            found = set(checkpoint.keys())
            if found != set(expected):
                raise WorkerError(
                    "invalid_weights",
                    f"PersonaPlex checkpoint keys differ: {len(found - set(expected))} "
                    f"unexpected, {len(set(expected) - found)} missing",
                )
            for name in checkpoint.keys():  # noqa: SIM118
                value = checkpoint.get_tensor(name)
                target = expected[name]
                if value.shape != target.shape or value.dtype != target.dtype:
                    raise WorkerError("invalid_weights", f"PersonaPlex tensor differs: {name}")
                check_deadline(deadline)
                target.copy_(value)
    else:
        lm = load_checkpoint_and_dispatch(
            lm,
            str(root / "model.safetensors"),
            device_map="auto" if execution["mode"] == "hybrid" else {"": execution["torch_device"]},
            max_memory=memory,
            no_split_module_classes=["StreamingTransformerLayer"],
            offload_folder=offload,
            offload_buffers=False,
            dtype=dtype,
            strict=True,
        )
    if execution["backend"] == "cuda":
        torch.cuda.empty_cache()
    mimi = loaders.get_mimi(
        root / "tokenizer-e351c8d8-checkpoint125.safetensors",
        device=execution["torch_device"],
    )
    lm.eval()
    gpu_layers = 0
    if execution["mode"] == "hybrid" and strategy == "cpu_compute":
        gpu_layers = int(os.environ.get("GEN_AUDIO_PERSONAPLEX_GPU_LAYERS", "8"))
        if not 0 <= gpu_layers <= len(lm.transformer.layers):
            raise WorkerError(
                "invalid_execution",
                f"GEN_AUDIO_PERSONAPLEX_GPU_LAYERS must be 0..{len(lm.transformer.layers)}",
            )

        def move(value, device):
            if torch.is_tensor(value):
                selected = (
                    torch.float16
                    if value.is_floating_point() and device.type == "cuda"
                    else torch.bfloat16
                    if value.is_floating_point()
                    else value.dtype
                )
                return value.to(device=device, dtype=selected)
            if isinstance(value, tuple):
                return tuple(move(item, device) for item in value)
            if isinstance(value, list):
                return [move(item, device) for item in value]
            if isinstance(value, Mapping):
                return {key: move(item, device) for key, item in value.items()}
            return value

        cuda = torch.device(execution["torch_device"])
        for index, layer in enumerate(lm.transformer.layers):
            device = cuda if index < gpu_layers else torch.device("cpu")
            layer.to(
                device=device,
                dtype=torch.float16 if device.type == "cuda" else dtype,
            )

            def transfer(current, args, kwargs, *, target=device):
                return move(args, target), move(kwargs, target)

            layer.register_forward_pre_hook(transfer, with_kwargs=True)
        lm.hf_device_map = {
            **{
                f"transformer.layers.{index}": (
                    execution["device_index"] if index < gpu_layers else "cpu"
                )
                for index in range(len(lm.transformer.layers))
            },
            "embeddings": "cpu",
            "depformer": "cpu",
            "audio_codec": execution["device_index"],
        }
        execution["components"] = {
            "language_model": {
                "precision": "bfloat16",
                "cpu_layers": len(lm.transformer.layers) - gpu_layers,
                "gpu_layers": gpu_layers,
            },
            "audio_codec": {
                "precision": "float32",
                "placement": execution["torch_device"],
            },
        }
    if execution["mode"] != "hybrid" and any(parameter.is_meta for parameter in lm.parameters()):
        raise WorkerError("invalid_weights", "PersonaPlex left unloaded meta parameters")
    if execution["mode"] == "hybrid" and strategy == "streaming_offload":
        from moshi.modules.transformer import (
            RingKVCache,
            StreamingMultiheadAttention,
            StreamingTransformer,
            _MHAState,
            _TransformerState,
        )

        def stream_device(module):
            for current in module.modules():
                hook = getattr(current, "_hf_hook", None)
                if hook is not None and hook.execution_device is not None:
                    return torch.device(hook.execution_device)
            return torch.device(execution["torch_device"])

        def streaming_attention_state(current, batch_size: int):
            device = stream_device(current)
            capacity = current.context if current.context is not None else current.weights_per_step
            if capacity is None:
                raise WorkerError("unsupported_model", "PersonaPlex attention has no streaming capacity")
            dim_per_head = current.embed_dim // current.num_heads
            cache = RingKVCache(
                batch_size,
                current.num_heads,
                dim_per_head,
                capacity,
                device,
                current.in_proj_weight.dtype,
            )
            return _MHAState(
                cache,
                offset=torch.zeros(1, device=device, dtype=torch.long),
                offset_cpu=0,
            )

        def streaming_transformer_state(current, batch_size: int):
            return _TransformerState(
                offset=torch.zeros(1, device=stream_device(current), dtype=torch.long)
            )

        for module in lm.modules():
            if isinstance(module, StreamingMultiheadAttention):
                module._init_streaming_state = MethodType(streaming_attention_state, module)
            elif isinstance(module, StreamingTransformer):
                module._init_streaming_state = MethodType(streaming_transformer_state, module)
    if mimi.sample_rate != SAMPLE_RATE or mimi.frame_rate != 12.5:
        raise WorkerError("unsupported_model", "Unexpected PersonaPlex codec rate")
    if execution["backend"] == "cuda":
        torch.cuda.synchronize(execution["device_index"])
        allocated = torch.cuda.memory_allocated(execution["device_index"])
        reserved = torch.cuda.memory_reserved(execution["device_index"])
    else:
        allocated = reserved = 0
    return {
        "lm": lm,
        "mimi": mimi,
        "tokenizer": tokenizer,
        "frame_size": int(mimi.sample_rate / mimi.frame_rate),
        "model_dir": root,
        "tokenizer_path": Path(report["assets"]["tokenizer"]).resolve(),
        "request": request,
        "execution": execution,
        "device_map": dict(getattr(lm, "hf_device_map", {})),
        "load_memory": {"allocated_bytes": allocated, "reserved_bytes": reserved},
        "placement_strategy": strategy,
        "cpu_threads": torch.get_num_threads(),
        "gpu_layers": gpu_layers,
    }


def generate(
    request: Request,
    report: dict,
    deadline: float,
    *,
    resident: dict | None = None,
) -> tuple:
    import numpy as np
    import torch
    from moshi.models import LMGen
    from moshi.models.lm import normalize_audio
    from moshi.utils.compile import no_compile, no_cuda_graph

    torch.manual_seed(request.seed)
    runtime = resident or load(request, report, deadline)
    if runtime["model_dir"] != request.model_dir.resolve() or runtime["tokenizer_path"] != Path(
        report["assets"]["tokenizer"]
    ).resolve():
        raise WorkerError("runtime_changed", "Loaded PersonaPlex checkpoint differs from the request; reload it")
    lm, mimi, tokenizer = runtime["lm"], runtime["mimi"], runtime["tokenizer"]
    frame_size = runtime["frame_size"]
    user_audio = read_pcm(request.input_audio, seconds=request.max_seconds, allow_silence=True)
    voice_audio = read_pcm(
        request.voice_refs[0],
        seconds=MAX_REFERENCE_SECONDS,
        resample=True,
    )
    voice_audio = checked_audio(
        normalize_audio(voice_audio, SAMPLE_RATE, -24.0),
        max_seconds=MAX_REFERENCE_SECONDS,
    )
    persona = request.persona.strip()
    if not (persona.startswith("<system>") and persona.endswith("<system>")):
        persona = f"<system> {persona} <system>"
    text_tokens = tokenizer.encode(persona)
    if not text_tokens or len(text_tokens) > MAX_INPUT_TOKENS:
        raise WorkerError("resource_limit", "Persona token count must be 1..4096")
    steps = math.ceil(user_audio.size / frame_size)
    if steps > request.max_new_tokens:
        raise WorkerError("resource_limit", "Conversation exceeds the stream-step cap")

    class BoundedLMGen(LMGen):
        def step(self, *args, **kwargs):
            check_deadline(deadline)
            target = self.lm_model.device
            moved_args = tuple(value.to(target) if hasattr(value, "to") else value for value in args)
            moved_kwargs = {
                key: value.to(target) if hasattr(value, "to") else value
                for key, value in kwargs.items()
            }
            return super().step(*moved_args, **moved_kwargs)

    generator = BoundedLMGen(
        lm,
        audio_silence_frame_cnt=int(0.5 * mimi.frame_rate),
        sample_rate=SAMPLE_RATE,
        device=report["device"],
        frame_rate=mimi.frame_rate,
        save_voice_prompt_embeddings=False,
        use_sampling=False,
        temp=0.8,
        temp_text=0.7,
        top_k=250,
        top_k_text=25,
    )
    frames: list[np.ndarray] = []
    generated_text: list[str] = []
    started = time.perf_counter()
    first_audio = None
    total = 0
    with torch.inference_mode(), no_compile(), no_cuda_graph(), mimi.streaming(1), generator.streaming(1):
        for _ in range(4):
            check_deadline(deadline)
            chunk = torch.zeros(1, 1, frame_size, dtype=torch.float32, device=report["device"])
            codes = mimi.encode(chunk)
            for index in range(codes.shape[-1]):
                tokens = generator.step(codes[:, :, index:index + 1])
                if tokens is not None:
                    mimi.decode(tokens[:, 1:9].to(report["device"]))
        generator.text_prompt_tokens = text_tokens
        mimi.reset_streaming()
        generator.reset_streaming()
        voice_cache_hit = _prime_voice_prompt(
            generator,
            mimi,
            voice_audio,
            _voice_cache_path(request.voice_refs[0], request.output),
        )
        mimi.reset_streaming()
        for offset in range(0, user_audio.size, frame_size):
            check_deadline(deadline)
            chunk = user_audio[offset:offset + frame_size]
            if chunk.size < frame_size:
                chunk = np.pad(chunk, (0, frame_size - chunk.size))
            codes = mimi.encode(
                torch.as_tensor(chunk, dtype=torch.float32, device=report["device"])[None, None, :]
            )
            for index in range(codes.shape[-1]):
                tokens = generator.step(codes[:, :, index:index + 1])
                if tokens is None:
                    continue
                pcm = mimi.decode(
                    tokens[:, 1:9].to(report["device"])
                ).detach().float().cpu().numpy()[0, 0]
                pcm = checked_audio(
                    pcm,
                    max_seconds=frame_size / SAMPLE_RATE,
                    allow_silence=True,
                )
                if first_audio is None:
                    first_audio = time.perf_counter() - started
                remaining = min(user_audio.size, int(request.max_seconds * SAMPLE_RATE)) - total
                if remaining <= 0:
                    raise WorkerError("resource_limit", "PersonaPlex generated more audio than the input")
                pcm = pcm[:remaining]
                frames.append(pcm)
                total += pcm.size
                text_token = tokens[0, 0, 0].item()
                if text_token not in (0, 1, 2, 3):
                    generated_text.append(tokenizer.id_to_piece(text_token).replace("▁", " "))
    generation_seconds = time.perf_counter() - started
    check_deadline(deadline)
    if not frames:
        raise WorkerError("no_generated_speech", "PersonaPlex returned no decoded frames")
    audio = checked_audio(np.concatenate(frames), max_seconds=request.max_seconds)
    if report["execution"]["backend"] == "cuda":
        torch.cuda.synchronize(report["execution"]["device_index"])
    return audio, {
        "input_audio": str(request.input_audio.resolve()),
        "persona_sha256": hashlib.sha256(persona.encode()).hexdigest(),
        "voice_references": [str(request.voice_refs[0].resolve())],
        "voice_format": "raw_pcm_wav",
        "stream_steps": steps,
        "codec_precision": "float32",
        "generated_text": "".join(generated_text),
        "generated_text_semantics": "model_response_not_input_transcription",
        "generation_seconds": generation_seconds,
        "time_to_first_audio_seconds": first_audio,
        "device_map": runtime["device_map"],
        "load_memory": runtime["load_memory"],
        "placement_strategy": runtime["placement_strategy"],
        "cpu_threads": runtime["cpu_threads"],
        "gpu_layers": runtime["gpu_layers"],
        "voice_cache_hit": voice_cache_hit,
    }


class PersonaPlexRealtime:
    """Reusable resident full-duplex Moshi stream for DLM Speech /api/chat."""

    def __init__(
        self,
        runtime: dict,
        *,
        voice_ref: Path,
        persona: str,
        seed: int = 0,
        audio_temperature: float = 0.8,
        text_temperature: float = 0.7,
        audio_top_k: int = 250,
        text_top_k: int = 25,
        timeout_seconds: float = 1_200,
    ) -> None:
        self.runtime = runtime
        self.voice_ref = voice_ref
        self.persona = persona.strip()
        self.seed = seed
        self.audio_temperature = audio_temperature
        self.text_temperature = text_temperature
        self.audio_top_k = audio_top_k
        self.text_top_k = text_top_k
        self.deadline = time.monotonic() + timeout_seconds
        self.stack: ExitStack | None = None
        self.pending = None
        self.started = 0.0
        self.generator = None

    def start(self) -> PersonaPlexRealtime:
        import numpy as np
        import torch
        from moshi.models import LMGen
        from moshi.models.lm import normalize_audio
        from moshi.utils.compile import no_compile, no_cuda_graph

        torch.manual_seed(self.seed)
        lm, mimi, tokenizer = (
            self.runtime["lm"],
            self.runtime["mimi"],
            self.runtime["tokenizer"],
        )
        persona = self.persona
        if not (persona.startswith("<system>") and persona.endswith("<system>")):
            persona = f"<system> {persona} <system>"
        tokens = tokenizer.encode(persona)
        if not tokens or len(tokens) > MAX_INPUT_TOKENS:
            raise WorkerError("resource_limit", "Persona token count must be 1..4096")
        voice = read_pcm(self.voice_ref, seconds=MAX_REFERENCE_SECONDS, resample=True)
        voice = checked_audio(
            normalize_audio(voice, SAMPLE_RATE, -24.0),
            max_seconds=MAX_REFERENCE_SECONDS,
        )

        class BoundedLMGen(LMGen):
            def step(stream, *args, **kwargs):
                check_deadline(self.deadline)
                target = stream.lm_model.device
                moved_args = tuple(value.to(target) if hasattr(value, "to") else value for value in args)
                moved_kwargs = {
                    key: value.to(target) if hasattr(value, "to") else value
                    for key, value in kwargs.items()
                }
                return super().step(*moved_args, **moved_kwargs)

        self.generator = BoundedLMGen(
            lm,
            audio_silence_frame_cnt=int(0.5 * mimi.frame_rate),
            sample_rate=SAMPLE_RATE,
            device=self.runtime["execution"]["torch_device"],
            frame_rate=mimi.frame_rate,
            save_voice_prompt_embeddings=False,
            use_sampling=True,
            temp=self.audio_temperature,
            temp_text=self.text_temperature,
            top_k=self.audio_top_k,
            top_k_text=self.text_top_k,
        )
        self.stack = ExitStack()
        self.stack.enter_context(torch.inference_mode())
        self.stack.enter_context(no_compile())
        self.stack.enter_context(no_cuda_graph())
        self.stack.enter_context(mimi.streaming(1))
        self.stack.enter_context(self.generator.streaming(1))
        frame_size = self.runtime["frame_size"]
        for _ in range(4):
            chunk = torch.zeros(
                1,
                1,
                frame_size,
                dtype=torch.float32,
                device=self.runtime["execution"]["torch_device"],
            )
            codes = mimi.encode(chunk)
            for index in range(codes.shape[-1]):
                generated = self.generator.step(codes[:, :, index:index + 1])
                if generated is not None:
                    mimi.decode(
                        generated[:, 1:9].to(self.runtime["execution"]["torch_device"])
                    )
        self.generator.text_prompt_tokens = tokens
        mimi.reset_streaming()
        self.generator.reset_streaming()
        self.voice_cache_hit = _prime_voice_prompt(
            self.generator,
            mimi,
            voice,
            _voice_cache_path(self.voice_ref, None),
        )
        mimi.reset_streaming()
        self.pending = np.empty(0, dtype=np.float32)
        self.started = time.perf_counter()
        return self

    def feed(self, audio):
        import numpy as np
        import torch

        if self.stack is None or self.generator is None:
            raise WorkerError("stream_not_started", "PersonaPlex stream has not started")
        values = np.asarray(audio, dtype=np.float32).reshape(-1)
        if self.pending is None:
            self.pending = np.empty(0, dtype=np.float32)
        pending = np.concatenate((self.pending, values))
        frame_size = self.runtime["frame_size"]
        complete = (pending.size // frame_size) * frame_size
        self.pending = pending[complete:]
        outputs = []
        text = []
        mimi, tokenizer = self.runtime["mimi"], self.runtime["tokenizer"]
        for offset in range(0, complete, frame_size):
            chunk = pending[offset:offset + frame_size]
            codes = mimi.encode(
                torch.as_tensor(
                    chunk,
                    dtype=torch.float32,
                    device=self.runtime["execution"]["torch_device"],
                )[None, None, :]
            )
            for index in range(codes.shape[-1]):
                tokens = self.generator.step(codes[:, :, index:index + 1])
                if tokens is None:
                    continue
                pcm = mimi.decode(
                    tokens[:, 1:9].to(self.runtime["execution"]["torch_device"])
                ).detach().float().cpu().numpy()[0, 0]
                outputs.append(checked_audio(
                    pcm,
                    max_seconds=frame_size / SAMPLE_RATE,
                    allow_silence=True,
                ))
                token = tokens[0, 0, 0].item()
                if token not in (0, 1, 2, 3):
                    text.append(tokenizer.id_to_piece(token).replace("▁", " "))
        return outputs, "".join(text), time.perf_counter() - self.started

    def close(self) -> None:
        if self.stack is not None:
            self.stack.close()
            self.stack = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.close()

