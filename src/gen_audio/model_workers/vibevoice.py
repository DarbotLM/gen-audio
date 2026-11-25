"""VibeVoice 7B direct offline adapter with bounded CUDA-to-RAM placement."""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import psutil

from .contracts import MAX_REFERENCE_SECONDS, SAMPLE_RATE, Request, WorkerError
from .execution import max_memory
from .preflight import normalized_script
from .runtime import check_deadline, checked_audio, read_pcm


def load(request: Request, report: dict, deadline: float) -> dict:
    import torch
    from accelerate import init_empty_weights
    from transformers.modeling_utils import no_init_weights
    from vibevoice.modular.configuration_vibevoice import VibeVoiceConfig
    from vibevoice.modular.modeling_vibevoice_inference import (
        VibeVoiceForConditionalGenerationInference,
    )
    from vibevoice.modular.modular_vibevoice_text_tokenizer import (
        VibeVoiceTextTokenizerFast,
    )
    from vibevoice.processor.vibevoice_processor import VibeVoiceProcessor
    from vibevoice.processor.vibevoice_tokenizer_processor import (
        VibeVoiceTokenizerProcessor,
    )

    torch.manual_seed(request.seed)
    configured_threads = int(os.environ.get("GEN_AUDIO_VIBEVOICE_CPU_THREADS", "24"))
    if not 1 <= configured_threads <= 32:
        raise WorkerError("invalid_execution", "GEN_AUDIO_VIBEVOICE_CPU_THREADS must be 1..32")
    torch.set_num_threads(min(configured_threads, psutil.cpu_count(logical=True) or configured_threads))
    execution = report["execution"]
    assets = report["assets"]
    tokenizer_path = Path(assets["tokenizer"])
    tokenizer = VibeVoiceTextTokenizerFast.from_pretrained(
        tokenizer_path,
        local_files_only=True,
        trust_remote_code=False,
    )
    vocab_size = int(assets["config"]["decoder_config"]["vocab_size"])
    special = (
        tokenizer.speech_start_id,
        tokenizer.speech_end_id,
        tokenizer.speech_diffusion_id,
        tokenizer.eos_token_id,
        tokenizer.pad_id,
    )
    if any(not isinstance(token, int) or not 0 <= token < vocab_size for token in special):
        raise WorkerError("incompatible_tokenizer", "Tokenizer special tokens do not fit the model vocabulary")
    if len(tokenizer) > vocab_size:
        raise WorkerError("incompatible_tokenizer", "Tokenizer exceeds the model embedding vocabulary")
    processor_config = assets["processor_config"]
    audio_config = processor_config.get("audio_processor", {})
    processor = VibeVoiceProcessor(
        tokenizer=tokenizer,
        audio_processor=VibeVoiceTokenizerProcessor(
            sampling_rate=SAMPLE_RATE,
            normalize_audio=audio_config.get("normalize_audio", True),
            target_dB_FS=audio_config.get("target_dB_FS", -25),
            eps=audio_config.get("eps", 1e-6),
        ),
        speech_tok_compress_ratio=3200,
        db_normalize=processor_config.get("db_normalize", True),
    )
    check_deadline(deadline)
    strategy = os.environ.get("GEN_AUDIO_VIBEVOICE_HYBRID_STRATEGY", "cpu_compute")
    if strategy not in {"cpu_compute", "streaming_offload"}:
        raise WorkerError("invalid_execution", "Unknown VibeVoice hybrid strategy")
    dtype = (
        torch.bfloat16
        if execution["mode"] == "hybrid" and strategy == "cpu_compute"
        else getattr(torch, execution["precision"])
    )
    config = VibeVoiceConfig.from_dict(deepcopy(assets["config"]))
    config.torch_dtype = dtype
    for name in (
        "decoder_config",
        "acoustic_tokenizer_config",
        "semantic_tokenizer_config",
        "diffusion_head_config",
    ):
        getattr(config, name).torch_dtype = dtype
    with init_empty_weights(include_buffers=False), no_init_weights():
        shape_model = VibeVoiceForConditionalGenerationInference(config)
    shape_model.tie_weights()
    del shape_model
    check_deadline(deadline)
    offload = Path(
        os.environ.get(
            "GEN_AUDIO_VIBEVOICE_OFFLOAD_DIR",
            str(Path(tempfile.gettempdir()) / "gen-audio-vibevoice-offload"),
        )
    )
    offload.mkdir(parents=True, exist_ok=True)
    attention = {"attn_implementation": "sdpa"} if execution["attention"] in {"auto", "math"} else {}
    loaded = VibeVoiceForConditionalGenerationInference.from_pretrained(
        request.model_dir.resolve(),
        torch_dtype=dtype,
        device_map="cpu" if execution["mode"] == "hybrid" else execution["torch_device"],
        low_cpu_mem_usage=True,
        use_safetensors=True,
        weights_only=True,
        local_files_only=True,
        trust_remote_code=False,
        output_loading_info=True,
        config=config,
        **attention,
    )
    model, loading = loaded if isinstance(loaded, tuple) else (loaded, {})
    if any(loading.get(key) for key in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs")):
        raise WorkerError("invalid_weights", "VibeVoice checkpoint did not load completely")
    model.eval()
    if any(parameter.is_meta for parameter in model.parameters()):
        raise WorkerError("invalid_weights", "VibeVoice left unloaded meta parameters")
    if execution["mode"] == "hybrid" and strategy == "streaming_offload":
        from accelerate import dispatch_model, infer_auto_device_map

        device_map = infer_auto_device_map(
            model,
            max_memory=max_memory(execution),
            dtype=dtype,
            no_split_module_classes=list(getattr(model, "_no_split_modules", set())),
            offload_buffers=True,
        )
        # Pin both audio codecs, the speech diffusion head, embeddings, and
        # output head so every prompt/decode step avoids whole-module CPU↔GPU
        # weight churn. Two early Qwen layers fit alongside them at the
        # 6.5 GiB qualification point; the remainder executes on host RAM.
        for name in tuple(device_map):
            if name.startswith("model.language_model.layers."):
                device_map[name] = "cpu"
        pinned = {
            "model.language_model.embed_tokens",
            "model.acoustic_tokenizer",
            "model.semantic_tokenizer",
            "model.acoustic_connector",
            "model.semantic_connector",
            "model.prediction_head",
            "lm_head",
        }
        for name in tuple(device_map):
            if name in pinned or any(
                name == f"model.language_model.layers.{index}"
                or name.startswith(f"model.language_model.layers.{index}.")
                for index in range(2)
            ):
                device_map[name] = execution["device_index"]
        # Accelerate 1.6 does not add zero-sized scalar buffers to an inferred
        # module map. Keep them on CPU explicitly; upstream code moves them to
        # the acoustic-latent device before use.
        device_map["model.speech_scaling_factor"] = "cpu"
        device_map["model.speech_bias_factor"] = "cpu"
        model = dispatch_model(
            model,
            device_map=device_map,
            offload_dir=offload,
            offload_buffers=True,
        )
    elif execution["mode"] == "hybrid":
        cuda = torch.device(execution["torch_device"])
        gpu_modules = (
            "model.acoustic_tokenizer",
            "model.semantic_tokenizer",
            "model.prediction_head",
        )

        def move(value, device, target_dtype):
            if torch.is_tensor(value):
                selected = target_dtype if value.is_floating_point() else value.dtype
                return value.to(device=device, dtype=selected)
            if isinstance(value, tuple):
                return tuple(move(item, device, target_dtype) for item in value)
            if isinstance(value, list):
                return [move(item, device, target_dtype) for item in value]
            if isinstance(value, Mapping):
                return {key: move(item, device, target_dtype) for key, item in value.items()}
            return value

        for name in gpu_modules:
            module = model.get_submodule(name)
            module.to(device=cuda, dtype=torch.float16)
            hook_dtype = torch.float16

            def pre_hook(current, args, kwargs, *, device=cuda, dtype=hook_dtype):
                return move(args, device, dtype), move(kwargs, device, dtype)

            module.register_forward_pre_hook(pre_hook, with_kwargs=True)
        for name in ("model.acoustic_tokenizer", "model.semantic_tokenizer"):
            codec = model.get_submodule(name)
            for part in ("encoder", "decoder"):
                submodule = getattr(codec, part, None)
                if submodule is None:
                    continue

                def codec_hook(current, args, kwargs, *, device=cuda):
                    return (
                        move(args, device, torch.float16),
                        move(kwargs, device, torch.float16),
                    )

                submodule.register_forward_pre_hook(codec_hook, with_kwargs=True)
        for name in ("model.acoustic_connector", "model.semantic_connector"):
            module = model.get_submodule(name)
            module.to(device="cpu", dtype=torch.bfloat16)

            def connector_hook(current, args, kwargs):
                return (
                    move(args, torch.device("cpu"), torch.bfloat16),
                    move(kwargs, torch.device("cpu"), torch.bfloat16),
                )

            module.register_forward_pre_hook(connector_hook, with_kwargs=True)
        model.hf_device_map = {
            "model.language_model": "cpu",
            **{name: execution["device_index"] for name in gpu_modules},
            "lm_head": "cpu",
        }
        execution["components"] = {
            "language_model": {"precision": "bfloat16", "placement": "cpu_ram"},
            "speech_codecs": {"precision": "float16", "placement": execution["torch_device"]},
            "diffusion_head": {"precision": "float16", "placement": execution["torch_device"]},
        }
    if execution["backend"] == "cuda":
        torch.cuda.synchronize(execution["device_index"])
        allocated = torch.cuda.memory_allocated(execution["device_index"])
        reserved = torch.cuda.memory_reserved(execution["device_index"])
    else:
        allocated = reserved = 0
    return {
        "model": model,
        "processor": processor,
        "tokenizer": tokenizer,
        "request": request,
        "execution": execution,
        "model_dir": request.model_dir.resolve(),
        "tokenizer_path": tokenizer_path,
        "device_map": dict(getattr(model, "hf_device_map", {})),
        "load_memory": {"allocated_bytes": allocated, "reserved_bytes": reserved},
        "placement_strategy": strategy,
        "cpu_threads": torch.get_num_threads(),
    }


def generate(
    request: Request,
    report: dict,
    deadline: float,
    *,
    resident: dict | None = None,
) -> tuple:
    import torch

    torch.manual_seed(request.seed)
    runtime = resident or load(request, report, deadline)
    if runtime["model_dir"] != request.model_dir.resolve() or runtime["tokenizer_path"] != Path(
        report["assets"]["tokenizer"]
    ):
        raise WorkerError("runtime_changed", "Loaded VibeVoice checkpoint differs from the request; reload it")
    model, processor, tokenizer = runtime["model"], runtime["processor"], runtime["tokenizer"]
    script = normalized_script(request.text or "", len(request.voice_refs))
    refs = [
        read_pcm(path, seconds=MAX_REFERENCE_SECONDS, resample=True)
        for path in request.voice_refs
    ]
    inputs = processor(
        text=[script],
        voice_samples=[refs],
        padding=True,
        return_tensors="pt",
        return_attention_mask=True,
    )
    count = int(inputs["input_ids"].shape[-1])
    token_cap = min(
        request.max_new_tokens,
        math.floor(request.max_seconds * SAMPLE_RATE / 3200),
    )
    max_position = int(report["assets"]["config"]["decoder_config"]["max_position_embeddings"])
    if count > max_position or count + token_cap > max_position:
        raise WorkerError("resource_limit", "Tokenized VibeVoice request exceeds the model context")
    device = model.device
    for name, value in inputs.items():
        if torch.is_tensor(value):
            if value.is_floating_point():
                inputs[name] = value.to(device=device, dtype=getattr(torch, report["dtype"]))
            else:
                inputs[name] = value.to(device=device)
    model.set_ddpm_inference_steps(num_steps=request.inference_steps)
    started = __import__("time").perf_counter()

    def stop_check():
        check_deadline(deadline)
        return False

    with torch.inference_mode():
        outputs = model.generate(
            **inputs,
            max_new_tokens=token_cap,
            tokenizer=tokenizer,
            is_prefill=True,
            cfg_scale=request.cfg_scale,
            generation_config={"do_sample": False},
            verbose=False,
            show_progress_bar=False,
            stop_check_fn=stop_check,
            max_length_times=token_cap,
        )
    generation_seconds = __import__("time").perf_counter() - started
    check_deadline(deadline)
    if not outputs.speech_outputs or outputs.speech_outputs[0] is None:
        raise WorkerError("no_generated_speech", "VibeVoice returned no decoded speech")
    audio = checked_audio(outputs.speech_outputs[0], max_seconds=request.max_seconds)
    generated_tokens = max(0, int(outputs.sequences.shape[-1]) - count)
    if report["execution"]["backend"] == "cuda":
        torch.cuda.synchronize(report["execution"]["device_index"])
    return audio, {
        "input_text_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "voice_references": [str(path) for path in request.voice_refs],
        "tokenizer": str(Path(report["assets"]["tokenizer"])),
        "input_tokens": count,
        "generated_tokens": generated_tokens,
        "effective_max_new_tokens": token_cap,
        "limit_reached": generated_tokens >= token_cap,
        "cfg_scale": request.cfg_scale,
        "ddpm_inference_steps": request.inference_steps,
        "generation_seconds": generation_seconds,
        "device_map": runtime["device_map"],
        "load_memory": runtime["load_memory"],
        "placement_strategy": runtime["placement_strategy"],
        "cpu_threads": runtime["cpu_threads"],
    }

