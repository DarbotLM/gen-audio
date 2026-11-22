"""Runtime hooks for Kokoro and Misaki.

These functions are the calls the synthesizers make when the optional
packages are installed. They do not download weights unless
``GEN_AUDIO_ALLOW_HF_DOWNLOAD=1``. Tests can call the pure pieces with fakes.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from gen_audio.voices.errors import VoiceError

_HF_ENV_LOCK = threading.Lock()


def allow_hf_download() -> bool:
    return os.environ.get("GEN_AUDIO_ALLOW_HF_DOWNLOAD", "").strip() == "1"


def resolve_voice_descriptor(voice_id: str | None):
    """Load a cast voice from an authored catalog or the checkout descriptor."""
    if not voice_id:
        return None
    from gen_audio.voices.catalog import load_catalog
    from gen_audio.voices.model import load_voice, repo_root

    catalog_raw = os.environ.get("GEN_AUDIO_VOICE_CATALOG", "").strip()
    if catalog_raw:
        return load_catalog(Path(catalog_raw)).voice(voice_id)
    framework, separator, local_id = voice_id.partition(".")
    if not separator or not framework or not local_id:
        raise VoiceError(
            f"voice_id {voice_id!r} must be framework.local-id",
            code="bad_voice_id",
        )
    checkout = repo_root() / "voices" / framework / f"{local_id}.json"
    if checkout.is_file():
        return load_voice(checkout)
    raise VoiceError(
        f"voice descriptor {voice_id} is not installed; set GEN_AUDIO_VOICE_CATALOG",
        code="missing_voice",
    )


def resolve_g2p_descriptor(g2p_id: str):
    """Load a G2P document from the authored catalog or shipped package data."""
    from gen_audio.voices.catalog import load_catalog
    from gen_audio.voices.model import g2p_config_path, load_g2p

    catalog_raw = os.environ.get("GEN_AUDIO_VOICE_CATALOG", "").strip()
    if catalog_raw:
        return load_catalog(Path(catalog_raw)).g2p_config(g2p_id)
    return load_g2p(g2p_config_path(g2p_id))


def kokoro_torch_kwargs(lang_code: str, *, device: str | None = None) -> dict[str, Any]:
    """Arguments for one DarbotLM ``KPipeline``."""
    if lang_code not in set("abefhijpz"):
        raise VoiceError(f"kokoro lang_code {lang_code} is not a pipeline code", code="bad_lang")
    return {
        "lang_code": lang_code,
        "repo_id": os.environ.get("GEN_AUDIO_KOKORO_REPO", "hexgrad/Kokoro-82M").strip()
        or "hexgrad/Kokoro-82M",
        "device": device or kokoro_torch_device(),
    }


def kokoro_torch_device() -> str:
    """Resolve ``auto``, CPU, CUDA, or MPS using the installed PyTorch fork."""
    raw = os.environ.get("GEN_AUDIO_KOKORO_DEVICE", "cpu").strip().lower()
    if raw not in {"", "auto", "cpu", "mps", "cuda"} and not raw.startswith("cuda:"):
        raise VoiceError(
            "GEN_AUDIO_KOKORO_DEVICE must be auto, cpu, cuda, cuda:N, or mps",
            code="bad_device",
        )
    try:
        import torch
    except ImportError as exc:
        raise VoiceError(
            "PyTorch is not installed; install gen-audio[kokoro-torch]",
            code="missing_kokoro",
        ) from exc
    if raw == "cpu":
        return "cpu"
    if raw.startswith("cuda"):
        if not torch.cuda.is_available():
            raise VoiceError(
                f"GEN_AUDIO_KOKORO_DEVICE is {raw} but CUDA is not available",
                code="bad_device",
            )
        return raw
    mps_backend = getattr(torch.backends, "mps", None)
    if raw == "mps":
        if mps_backend is None or not mps_backend.is_available():
            raise VoiceError(
                "GEN_AUDIO_KOKORO_DEVICE is mps but Apple MPS is not available",
                code="bad_device",
            )
        if os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK") != "1":
            raise VoiceError(
                "MPS needs PYTORCH_ENABLE_MPS_FALLBACK=1",
                code="bad_device",
            )
        return raw
    if torch.cuda.is_available():
        return "cuda"
    if (
        mps_backend is not None
        and mps_backend.is_available()
        and os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK") == "1"
    ):
        return "mps"
    return "cpu"


@contextmanager
def hf_offline_when_required():
    """Make every Hugging Face lookup fail on missing cache instead of networking."""
    if allow_hf_download():
        yield
        return
    with _HF_ENV_LOCK:
        saved = {
            key: os.environ.get(key)
            for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
        }
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        hub_constants = None
        previous_hub_offline = None
        try:
            from huggingface_hub import constants as hub_constants

            previous_hub_offline = bool(hub_constants.HF_HUB_OFFLINE)
            hub_constants.HF_HUB_OFFLINE = True
        except ImportError:
            pass
        try:
            yield
        finally:
            if hub_constants is not None and previous_hub_offline is not None:
                hub_constants.HF_HUB_OFFLINE = previous_hub_offline
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


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


def build_misaki(config, *, library: Path | str | None = None):
    """Construct the Misaki backend named by ``config``.

    ``transformer`` must be false. A missing misaki install is a refusal,
    not a silent espeak fallback. ``custom_entries`` and an optional JSON or
    line-oriented lexicon land in ``lexicon.golds``.
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

            g2p = ja.JAG2P(
                version=config.version or "pyopenjtalk",
                unk=config.unk or "❓",
            )
        elif config.backend == "misaki-zh":
            from misaki import en, zh

            version = None if config.version in {None, "legacy"} else config.version
            english = None
            if version == "1.1":
                english_g2p = en.G2P(trf=False, british=False, fallback=None, unk="")

                def english(text: str) -> str:
                    phonemes, _tokens = english_g2p(text)
                    return phonemes

            g2p = zh.ZHG2P(
                version=version,
                unk=config.unk or "❓",
                en_callable=english,
            )
        elif config.backend == "misaki-ko":
            from misaki import ko

            g2p = ko.KOG2P()
        elif config.backend == "misaki-vi":
            from misaki import vi

            g2p = vi.VIG2P(enable_en_g2p=bool(config.enable_en_g2p))
        elif config.backend == "espeak":
            from misaki import espeak

            g2p = espeak.EspeakG2P(language=config.espeak_language or config.language)
        else:
            raise VoiceError(f"no misaki backend {config.backend}", code="bad_lang")
    except ImportError as exc:
        dependency = getattr(exc, "name", None) or str(exc)
        raise VoiceError(
            f"Misaki backend {config.backend} dependency is not installed: {dependency}",
            code="missing_misaki",
        ) from exc
    entries = {}
    if config.lexicon:
        entries.update(_load_custom_lexicon(config.lexicon, library))
    if config.custom_entries:
        entries.update(config.custom_entries)
    if entries:
        apply_custom_entries(g2p, entries)
    return g2p


def _load_custom_lexicon(relative: str, library: Path | str | None) -> dict[str, str]:
    raw_root = str(library).strip() if library is not None else ""
    raw_root = raw_root or os.environ.get("GENAID_VOICES_DIR", "").strip()
    if not raw_root:
        raise VoiceError(
            "Misaki lexicon is configured but GENAID_VOICES_DIR is unset",
            code="missing_library",
        )
    root = Path(raw_root).resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise VoiceError(f"Misaki lexicon escapes the voice library: {relative}", code="bad_path")
    if not path.is_file():
        raise VoiceError(f"Misaki lexicon file is missing: {relative}", code="missing_lexicon")
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
        ):
            raise VoiceError("Misaki JSON lexicon must be an object of strings", code="bad_lexicon")
        return dict(payload)
    entries: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        separator = "\t" if "\t" in line else "="
        key, found, value = line.partition(separator)
        if not found or not key.strip() or not value.strip():
            raise VoiceError(
                "Misaki line lexicon entries need <word><tab><phonemes> or <word>=<phonemes>",
                code="bad_lexicon",
            )
        entries[key.strip()] = value.strip()
    return entries


def phonemize_with_misaki(text: str, config, *, library: Path | str | None = None) -> str:
    """Return Misaki phonemes for ``text``. Kokoro then calls ``create(..., is_phonemes=True)``."""
    g2p = build_misaki(config, library=library)
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
    return voice_id


def _construct_pipeline(pipeline_cls, kwargs: dict[str, Any]):
    return pipeline_cls(**kwargs)


def _result_audio(result):
    if hasattr(result, "audio"):
        return result.audio
    if isinstance(result, tuple) and len(result) >= 3:
        return result[2]
    raise VoiceError("KPipeline result did not include audio", code="bad_load")


def _require_model_file(raw: str, env_name: str) -> str:
    path = Path(raw).expanduser()
    base = os.environ.get("GENAID_MODEL_BASE", "").strip()
    if base:
        from gen_audio.guards import resolve_model_directory, verify_model_checksums

        confined = Path(resolve_model_directory(str(path.parent), base))
        path = confined / path.name
        verify_model_checksums(str(confined))
    if not path.is_file():
        raise VoiceError(f"{env_name} file is missing: {path}", code="bad_load")
    return str(path)


def build_kokoro_model(*, repo_id: str | None = None, device: str | None = None, model_cls=None):
    """Load one Kokoro model, from explicit local files or controlled HF cache."""
    with hf_offline_when_required():
        if model_cls is None:
            try:
                from kokoro import KModel as model_cls
            except ImportError as exc:
                raise VoiceError(
                    "kokoro package is not installed; install gen-audio[kokoro-torch]",
                    code="missing_kokoro",
                ) from exc
        selected_repo = repo_id or kokoro_torch_kwargs("a")["repo_id"]
        selected_device = device or kokoro_torch_device()
        config_raw = os.environ.get("GEN_AUDIO_KOKORO_TORCH_CONFIG", "").strip()
        weights_raw = os.environ.get("GEN_AUDIO_KOKORO_TORCH_WEIGHTS", "").strip()
        if bool(config_raw) != bool(weights_raw):
            raise VoiceError(
                "GEN_AUDIO_KOKORO_TORCH_CONFIG and GEN_AUDIO_KOKORO_TORCH_WEIGHTS must be set together",
                code="bad_load",
            )
        kwargs: dict[str, Any] = {"repo_id": selected_repo}
        if config_raw:
            kwargs["config"] = _require_model_file(
                config_raw, "GEN_AUDIO_KOKORO_TORCH_CONFIG"
            )
            kwargs["model"] = _require_model_file(
                weights_raw, "GEN_AUDIO_KOKORO_TORCH_WEIGHTS"
            )
        disable_complex = os.environ.get("GEN_AUDIO_KOKORO_DISABLE_COMPLEX", "").strip()
        if disable_complex:
            if disable_complex not in {"0", "1"}:
                raise VoiceError(
                    "GEN_AUDIO_KOKORO_DISABLE_COMPLEX must be 0 or 1",
                    code="bad_device",
                )
            kwargs["disable_complex"] = disable_complex == "1"
        model = model_cls(**kwargs)
    return model.to(selected_device).eval()


def run_kokoro_pipeline(
    text: str,
    *,
    lang_code: str,
    voice,
    g2p,
    speed: float,
    pipeline_cls=None,
    pipeline=None,
    model=None,
    library: Path | str | None = None,
):    """Call ``KPipeline`` with a Misaki ``g2p`` assigned and return 24 kHz audio.

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
    if pipeline is None:
        kwargs = kokoro_torch_kwargs(lang_code)
        if model is not None:
            kwargs["model"] = model
        pipeline = _construct_pipeline(pipeline_cls, kwargs)
    pipeline.g2p = build_misaki(g2p, library=library)    chunks = []
    with hf_offline_when_required():
        for result in pipeline(text, voice=voice, speed=float(speed)):
            audio = _result_audio(result)
            if audio is None:
                raise VoiceError("KPipeline result did not include audio", code="empty_audio")
            chunks.append(np.asarray(audio, dtype=np.float32).reshape(-1))
    if not chunks:
        raise VoiceError("KPipeline produced no audio", code="empty_audio")
    return np.concatenate(chunks), 24000


class KokoroTorchSession:
    """One shared KModel and one KPipeline per language."""

    def __init__(self) -> None:
        self.repo_id = kokoro_torch_kwargs("a")["repo_id"]
        self.device = kokoro_torch_device()
        self.model = build_kokoro_model(
            repo_id=self.repo_id,
            device=self.device,
        )
        self.pipelines: dict[str, Any] = {}

    def pipeline_for(self, lang_code: str, g2p, *, library: Path | str | None = None):
        from kokoro import KPipeline

        pipeline = self.pipelines.get(lang_code)
        if pipeline is None:
            kwargs = kokoro_torch_kwargs(lang_code, device=self.device)
            kwargs["model"] = self.model
            with hf_offline_when_required():
                pipeline = _construct_pipeline(KPipeline, kwargs)
            self.pipelines[lang_code] = pipeline
        pipeline.g2p = build_misaki(g2p, library=library)
        return pipeline

    def stream(
        self,
        text: str,
        *,
        lang_code: str,
        voice,
        g2p,
        speed: float,
        library: Path | str | None = None,
    ):
        """Yield each real KPipeline audio chunk with its 24 kHz sample rate."""
        import numpy as np

        pipeline = self.pipeline_for(lang_code, g2p, library=library)
        with hf_offline_when_required():
            for result in pipeline(text, voice=voice, speed=float(speed)):
                audio = _result_audio(result)
                if audio is None:
                    raise VoiceError(
                        "KPipeline result did not include audio",
                        code="empty_audio",
                    )
                yield np.asarray(audio, dtype=np.float32).reshape(-1), 24000

    def synthesize(
        self,
        text: str,
        *,
        lang_code: str,
        voice,
        g2p,
        speed: float,
        library: Path | str | None = None,
    ):
        import numpy as np

        chunks = [
            audio
            for audio, _sample_rate in self.stream(
                text,
                lang_code=lang_code,
                voice=voice,
                g2p=g2p,
                speed=speed,
                library=library,
            )
        ]
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
        library=library,
    )
