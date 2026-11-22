"""Runtime hooks for Kokoro and Misaki.

These functions are the calls the synthesizers make when the optional
packages are installed. They do not download weights unless
``GEN_AUDIO_ALLOW_HF_DOWNLOAD=1``. Tests can call the pure pieces with fakes.
"""

from __future__ import annotations

import os
from typing import Any

from gen_audio.voices.errors import VoiceError


def allow_hf_download() -> bool:
    return os.environ.get("GEN_AUDIO_ALLOW_HF_DOWNLOAD", "").strip() == "1"


def kokoro_torch_kwargs(lang_code: str, *, device: str = "cpu") -> dict[str, Any]:
    """Arguments for ``KPipeline``. The 82M model is the CPU path."""
    if lang_code not in set("abefhijpz"):
        raise VoiceError(f"kokoro lang_code {lang_code} is not a pipeline code", code="bad_lang")
    return {
        "lang_code": lang_code,
        "repo_id": "hexgrad/Kokoro-82M",
        "device": device,
        "local_files_only": not allow_hf_download(),
    }


def mix_kokoro_packs(packs: list[tuple[Any, float]]):
    """Weighted sum of Kokoro ``.pt`` tensors. ``KPipeline.load_voice`` is equal-weight only."""
    if len(packs) < 2:
        raise VoiceError("a kokoro blend needs at least two packs", code="bad_blend")
    mixed = None
    for tensor, weight in packs:
        scaled = tensor * float(weight)
        mixed = scaled if mixed is None else mixed + scaled
    return mixed


def apply_custom_entries(g2p: Any, entries: dict[str, str]) -> Any:
    """Write ``custom_entries`` into ``g2p.lexicon.golds``. Misaki has no other hook."""
    lexicon = getattr(g2p, "lexicon", None)
    golds = getattr(lexicon, "golds", None)
    if not isinstance(golds, dict):
        raise VoiceError("misaki G2P has no lexicon.golds for custom_entries", code="no_lexicon")
    golds.update(entries)
    return g2p


def build_misaki(config):
    """Construct the Misaki backend named by ``config``.

    ``transformer`` must be false. A missing misaki install is a refusal,
    not a silent espeak fallback. ``custom_entries`` land in ``lexicon.golds``.
    """
    if config.transformer:
        raise VoiceError("misaki transformer=true is rejected on Python 3.14", code="bad_transformer")
    try:
        if config.backend == "misaki-en":
            from misaki import en, espeak

            fallback = espeak.EspeakFallback(british=bool(config.british)) if config.espeak_fallback else None
            g2p = en.G2P(trf=False, british=bool(config.british), fallback=fallback, unk=config.unk or "")
        elif config.backend == "misaki-ja":
            from misaki import ja

            g2p = ja.JAG2P()
        elif config.backend == "misaki-zh":
            from misaki import zh

            g2p = zh.ZHG2P()
        elif config.backend == "espeak":
            from misaki import espeak

            g2p = espeak.EspeakG2P(language=config.espeak_language or config.language)
        else:
            raise VoiceError(f"no misaki backend {config.backend}", code="bad_lang")
    except ImportError as exc:
        raise VoiceError(
            "misaki is not installed; kokoro will not invent phonemes",
            code="missing_misaki",
        ) from exc
    if config.custom_entries:
        apply_custom_entries(g2p, dict(config.custom_entries))
    return g2p


def phonemize_with_misaki(text: str, config) -> str:
    """Return Misaki phonemes for ``text``. Kokoro then calls ``create(..., is_phonemes=True)``."""
    g2p = build_misaki(config)
    phonemes, _tokens = g2p(text)
    if not isinstance(phonemes, str) or not phonemes:
        raise VoiceError("misaki returned no phonemes", code="empty_phonemes")
    return phonemes


def split_phonemes(phonemes: str, limit: int = 510) -> list[str]:
    """Keep each kokoro-onnx chunk at or under ``limit`` phoneme characters."""
    if limit < 1:
        raise VoiceError("phoneme chunk limit must be positive", code="empty_phonemes")
    if len(phonemes) <= limit:
        return [phonemes]
    chunks: list[str] = []
    current = ""
    for token in phonemes.split(" "):
        piece = token if not current else f"{current} {token}"
        if len(piece) <= limit:
            current = piece
            continue
        if current:
            chunks.append(current)
        if len(token) <= limit:
            current = token
            continue
        for start in range(0, len(token), limit):
            chunks.append(token[start : start + limit])
        current = ""
    if current:
        chunks.append(current)
    return chunks


def kokoro_torch_voice_argument(voice, library, torch_load):
    """Local ``.pt`` path, pack id when download is allowed, or a weighted tensor for a blend.

    ``KPipeline.load_voice`` averages ids with equal weight. A blend therefore
    passes ``Σ wᵢ·packᵢ`` and never a comma-joined id.
    """
    from pathlib import Path

    params = voice.params
    if "blend" in params:
        if library is None:
            raise VoiceError("a kokoro blend needs the local .pt packs", code="bad_blend")
        mixed_from = []
        for part in params["blend"]:
            asset = next(item for item in voice.assets if item.member == part["voice_id"])
            path = Path(library) / asset.path
            if not path.is_file():
                raise VoiceError(f"Kokoro pack is not on disk: {asset.path}", code="bad_load")
            mixed_from.append((torch_load(path), float(part["weight"])))
        return mix_kokoro_packs(mixed_from)
    voice_id = params["voice_id"]
    if library is not None:
        asset = next(item for item in voice.assets if item.member == voice_id)
        path = Path(library) / asset.path
        if path.is_file():
            return str(path)
    if allow_hf_download():
        return voice_id
    raise VoiceError(
        "Kokoro .pt packs are not bundled; GEN_AUDIO_ALLOW_HF_DOWNLOAD=1 is required before KPipeline can fetch hexgrad/Kokoro-82M",
        code="bad_load",
    )


def _construct_pipeline(pipeline_cls, kwargs: dict[str, Any]):
    try:
        return pipeline_cls(**kwargs)
    except TypeError as exc:
        if "local_files_only" not in str(exc):
            raise
        slim = {key: value for key, value in kwargs.items() if key != "local_files_only"}
        return pipeline_cls(**slim)


def _result_audio(result):
    if hasattr(result, "audio"):
        return result.audio
    if isinstance(result, tuple) and len(result) >= 3:
        return result[2]
    raise VoiceError("KPipeline result did not include audio", code="bad_load")


def kokoro_torch_dir():
    """``GEN_AUDIO_KOKORO_TORCH_DIR``: a local hexgrad/Kokoro-82M copy (config.json,
    kokoro-v1_0.pth, voices/*.pt), or ``None``."""
    from pathlib import Path

    raw = os.environ.get("GEN_AUDIO_KOKORO_TORCH_DIR", "").strip()
    return Path(raw) if raw else None


def kokoro_pack_path(voice_id: str):
    """Local ``.pt`` pack for ``voice_id``: the torch dir, then ``GENAID_VOICES_DIR/kokoro/voices``."""
    from pathlib import Path

    roots = []
    folder = kokoro_torch_dir()
    if folder is not None:
        roots.append(folder / "voices")
    library = os.environ.get("GENAID_VOICES_DIR", "").strip()
    if library:
        roots.append(Path(library) / "kokoro" / "voices")
    for root in roots:
        path = root / f"{voice_id}.pt"
        if path.is_file():
            return path
    return None


def blend_kokoro_packs(parts: list[tuple[str, float]], torch_load=None):
    """``Σ wᵢ·packᵢ`` over local ``.pt`` packs for a voices/agents blend."""
    if torch_load is None:
        import torch

        def torch_load(path):
            return torch.load(path, weights_only=True)

    packs = []
    for voice_id, weight in parts:
        path = kokoro_pack_path(voice_id)
        if path is None:
            raise VoiceError(
                f"Kokoro pack {voice_id}.pt is not in GEN_AUDIO_KOKORO_TORCH_DIR/voices or GENAID_VOICES_DIR",
                code="bad_load",
            )
        packs.append((torch_load(path), weight))
    return mix_kokoro_packs(packs)


_LOCAL_PIPELINES: dict = {}


def _local_pipeline(lang_code: str, pipeline_cls):
    """One KPipeline per language on the local weights; loading the model per turn is slow."""
    folder = kokoro_torch_dir()
    key = (lang_code, str(folder))
    if key not in _LOCAL_PIPELINES:
        config = folder / "config.json"
        weights = folder / "kokoro-v1_0.pth"
        if not config.is_file() or not weights.is_file():
            raise VoiceError(
                f"GEN_AUDIO_KOKORO_TORCH_DIR {folder} needs config.json and kokoro-v1_0.pth from hexgrad/Kokoro-82M",
                code="bad_load",
            )
        from kokoro import KModel

        model = KModel(repo_id="hexgrad/Kokoro-82M", config=str(config), model=str(weights)).eval()
        kwargs = kokoro_torch_kwargs(lang_code)
        kwargs["model"] = model
        _LOCAL_PIPELINES[key] = _construct_pipeline(pipeline_cls, kwargs)
    return _LOCAL_PIPELINES[key]


def run_kokoro_pipeline(text: str, *, lang_code: str, voice, g2p, speed: float, pipeline_cls=None):
    """Call ``KPipeline`` with a Misaki ``g2p`` assigned and return 24 kHz audio.

    ``voice`` is a pack id, a local ``.pt`` path, or a pre-mixed tensor. With
    ``GEN_AUDIO_KOKORO_TORCH_DIR`` set (and no injected ``pipeline_cls``) the
    model loads from that directory, never from the network.
    """
    import numpy as np

    if pipeline_cls is None:
        try:
            from kokoro import KPipeline as pipeline_cls
        except ImportError as exc:
            raise VoiceError("kokoro package is not installed", code="missing_kokoro") from exc
        if kokoro_torch_dir() is not None:
            pipeline = _local_pipeline(lang_code, pipeline_cls)
        else:
            pipeline = _construct_pipeline(pipeline_cls, kokoro_torch_kwargs(lang_code))
    else:
        pipeline = _construct_pipeline(pipeline_cls, kokoro_torch_kwargs(lang_code))
    pipeline.g2p = build_misaki(g2p)
    chunks = []
    for result in pipeline(text, voice=voice, speed=float(speed)):
        chunks.append(np.asarray(_result_audio(result), dtype=np.float32).reshape(-1))
    if not chunks:
        raise VoiceError("KPipeline produced no audio", code="empty_audio")
    return np.concatenate(chunks), 24000


def run_kokoro_voice(text: str, voice, g2p, library, *, pipeline_cls=None, torch_load=None):
    """Synthesize one Kokoro torch voice, mixing a blend before ``KPipeline``."""
    if torch_load is None:
        import torch

        def torch_load(path):
            return torch.load(path, weights_only=True)

    argument = kokoro_torch_voice_argument(voice, library, torch_load)
    return run_kokoro_pipeline(
        text,
        lang_code=voice.params["lang_code"],
        voice=argument,
        g2p=g2p,
        speed=float(voice.params["speed"]),
        pipeline_cls=pipeline_cls,
    )
