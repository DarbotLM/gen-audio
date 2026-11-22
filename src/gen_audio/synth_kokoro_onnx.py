"""Multi-turn WAV synthesis through kokoro-onnx.

The optional extra is imported only when a synthesizer is constructed.
Model files stay on disk; this module does not download them.

Expected files from the kokoro-onnx v1 setup are typically ``kokoro-v1.0.onnx``
and ``voices-v1.0.bin``. Point at whatever paths you actually have:

* ``--model`` / ``--voices``
* or ``GEN_AUDIO_KOKORO_MODEL`` and ``GEN_AUDIO_KOKORO_VOICES``

The cast map engine field must be ``kokoro_onnx``. Example voices in
``voices/cast_map.example.json`` are ``af_heart`` (Alice) and ``am_michael``
(Frank). kokoro-onnx rejects a voice id that is not in the loaded voice pack.
"""

from __future__ import annotations

import os
import threading
from math import isfinite
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from gen_audio.cast import CastMap, Turn, assignment_framework, resolve_turn
from gen_audio.guards import (
    local_files_only_enabled,
    resolve_model_directory,
    verify_model_checksums,
)
from gen_audio.improve import improve, resample_audio
from gen_audio.speakers import SpeechSegment, concatenate_turns, published_segments, remap_through_improve, roster

# One generate at a time. Unload and disk IO stay off this lock's caller thread
# by running through ``run_blocking`` in ``gen_audio.gateway``.
_MODEL_LOCK = threading.Lock()

DEFAULT_TURN_GAP_S = 0.35
DEFAULT_SENTENCE_PAUSE_S = 0.25
DEFAULT_CLAUSE_PAUSE_S = 0.1

_MISAKI_G2P_BY_LANG = {
    "en-us": "misaki.en-us",
    "en-gb": "misaki.en-gb",
    "es": "misaki.es",
    "fr-fr": "misaki.fr-fr",
    "hi": "misaki.hi",
    "it": "misaki.it",
    "pt-br": "misaki.pt-br",
    "ja": "misaki.ja",
    "zh": "misaki.zh",
    "ko": "misaki.ko",
    "vi": "misaki.vi",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _env_pause(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    value = float(raw) if raw else default
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return value


@dataclass(frozen=True)
class OnnxSynthesisOptions:
    """Fork-0.6.1 synthesis controls that are safe to put on every public path."""

    trim: bool = True
    sentence_pause: float = DEFAULT_SENTENCE_PAUSE_S
    clause_pause: float = DEFAULT_CLAUSE_PAUSE_S
    continuous: bool = False
    use_misaki: bool = False

    def __post_init__(self) -> None:
        if (
            not isfinite(self.sentence_pause)
            or not isfinite(self.clause_pause)
            or self.sentence_pause < 0
            or self.clause_pause < 0
        ):
            raise ValueError("sentence and clause pauses must be finite numbers >= 0")

    @classmethod
    def from_env(cls) -> "OnnxSynthesisOptions":
        return cls(
            trim=_env_bool("GEN_AUDIO_KOKORO_TRIM", True),
            sentence_pause=_env_pause(
                "GEN_AUDIO_KOKORO_SENTENCE_PAUSE", DEFAULT_SENTENCE_PAUSE_S
            ),
            clause_pause=_env_pause(
                "GEN_AUDIO_KOKORO_CLAUSE_PAUSE", DEFAULT_CLAUSE_PAUSE_S
            ),
            continuous=_env_bool("GEN_AUDIO_KOKORO_CONTINUOUS", False),
            use_misaki=_env_bool("GEN_AUDIO_KOKORO_USE_MISAKI", False),
        )


@dataclass(frozen=True)
class SynthTurn:
    """One synthesized turn, before the shared gap that follows it."""

    speaker_id: str
    name: str
    voice: str
    lang: str
    speed: float
    text: str
    samples: int
    sample_rate: int
    timings: list[dict[str, float | str]] = field(default_factory=list)


@dataclass(frozen=True)
class SynthResult:
    """Concatenated mono render plus a small manifest."""

    audio: np.ndarray
    sample_rate: int
    turns: list[SynthTurn] = field(default_factory=list)
    gap_s: float = DEFAULT_TURN_GAP_S
    improved: bool = False
    speakers: list[dict] = field(default_factory=list)
    segments: list[SpeechSegment] = field(default_factory=list)
    phoneme_timings: list[dict[str, int | float | str]] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)

    def manifest(self) -> dict:
        return {
            "engine": "kokoro_onnx",
            "sample_rate": self.sample_rate,
            "samples": int(self.audio.size),
            "seconds": (float(self.audio.size) / float(self.sample_rate)) if self.sample_rate else 0.0,
            "gap_s": self.gap_s,
            "improved": self.improved,
            "speakers": list(self.speakers),
            "segments": published_segments(self.segments, self.sample_rate),
            "phoneme_timings": list(self.phoneme_timings),
            "providers": list(self.providers),
            "turns": [
                {
                    "speaker_id": turn.speaker_id,
                    "name": turn.name,
                    "voice": turn.voice,
                    "lang": turn.lang,
                    "speed": turn.speed,
                    "text": turn.text,
                    "samples": turn.samples,
                    "sample_rate": turn.sample_rate,
                    "timings": list(turn.timings),
                }
                for turn in self.turns
            ],
        }


def resolve_model_paths(
    model: str | Path | None = None,
    voices: str | Path | None = None,
) -> tuple[Path, Path]:
    """Resolve model and voice-pack paths from arguments or the environment."""
    model_raw = model or os.environ.get("GEN_AUDIO_KOKORO_MODEL")
    voices_raw = voices or os.environ.get("GEN_AUDIO_KOKORO_VOICES")
    if not model_raw or not voices_raw:
        raise RuntimeError(
            "kokoro-onnx model paths are missing. Pass --model and --voices, or set "
            "GEN_AUDIO_KOKORO_MODEL and GEN_AUDIO_KOKORO_VOICES. This package does not download weights."
        )
    model_path = Path(model_raw)
    voices_path = Path(voices_raw)
    if local_files_only_enabled():
        for raw in (str(model_path), str(voices_path)):
            lowered = raw.replace("\\", "/").lower()
            if "://" in lowered or lowered.startswith("file:"):
                raise ValueError("GENAID_LOCAL_FILES_ONLY rejects non-local model paths")
    base = os.environ.get("GENAID_MODEL_BASE", "").strip()
    if base:
        model_dir = model_path if model_path.is_dir() else model_path.parent
        confined = Path(resolve_model_directory(str(model_dir), base))
        if not model_path.is_dir():
            model_path = confined / model_path.name
        verify_model_checksums(str(confined))
    return model_path, voices_path


class KokoroOnnxSynthesizer:
    """Lazy wrapper around ``kokoro_onnx.Kokoro``."""

    def __init__(
        self,
        model_path: str | Path,
        voices_path: str | Path,
        *,
        vocab_config: dict | str | Path | None = None,
        espeak_lib: str | Path | None = None,
        espeak_data: str | Path | None = None,
        provider: str | None = None,
        session: Any | None = None,
    ) -> None:
        model = Path(model_path)
        voices = Path(voices_path)
        if session is None and not model.is_file():
            raise FileNotFoundError(f"kokoro-onnx model file not found: {model}")
        if not voices.is_file():
            raise FileNotFoundError(f"kokoro-onnx voices file not found: {voices}")
        try:
            from kokoro_onnx import Kokoro
            from kokoro_onnx.config import EspeakConfig
        except ImportError as exc:
            raise ImportError(
                "kokoro-onnx is not installed. Install the optional extra: "
                "pip install 'gen-audio[kokoro-onnx]'"
            ) from exc
        self._voices_path = voices
        selected_vocab = vocab_config or os.environ.get("GEN_AUDIO_KOKORO_VOCAB", "").strip() or None
        if isinstance(selected_vocab, Path):
            selected_vocab = str(selected_vocab)
        if isinstance(selected_vocab, str) and not Path(selected_vocab).is_file():
            raise FileNotFoundError(f"kokoro-onnx vocabulary config not found: {selected_vocab}")
        if isinstance(selected_vocab, str) and os.environ.get(
            "GENAID_MODEL_BASE", ""
        ).strip():
            from gen_audio.voices.runtime import _require_model_file

            selected_vocab = _require_model_file(
                selected_vocab, "GEN_AUDIO_KOKORO_VOCAB"
            )
        lib_raw = espeak_lib or os.environ.get("GEN_AUDIO_KOKORO_ESPEAK_LIB", "").strip() or None
        data_raw = espeak_data or os.environ.get("GEN_AUDIO_KOKORO_ESPEAK_DATA", "").strip() or None
        espeak = EspeakConfig(
            lib_path=str(lib_raw) if lib_raw else None,
            data_path=str(data_raw) if data_raw else None,
        )
        previous_provider = os.environ.get("ONNX_PROVIDER")
        if provider:
            os.environ["ONNX_PROVIDER"] = provider
        try:
            if session is not None:
                self._kokoro = Kokoro.from_session(
                    session,
                    str(voices),
                    espeak_config=espeak,
                    vocab_config=selected_vocab,
                )
            else:
                self._kokoro = Kokoro(
                    str(model),
                    str(voices),
                    espeak_config=espeak,
                    vocab_config=selected_vocab,
                )
        finally:
            if provider:
                if previous_provider is None:
                    os.environ.pop("ONNX_PROVIDER", None)
                else:
                    os.environ["ONNX_PROVIDER"] = previous_provider

    @property
    def providers(self) -> list[str]:
        session = getattr(self._kokoro, "sess", None)
        return list(session.get_providers()) if session is not None else []

    def voices(self) -> list[str]:
        get_voices = getattr(self._kokoro, "get_voices", None)
        if get_voices is None:
            raise RuntimeError("installed kokoro-onnx does not expose get_voices")
        return list(get_voices())

    def voice_for_create(self, params: dict):
        """Pack id, or a blended style vector when ``params`` names a blend.

        kokoro-onnx accepts the ndarray directly. Cast speakers with a
        descriptor ``voice_id`` use the same path as direct SDK callers.
        """
        from gen_audio.voices.blend import blend_from_pack

        if "blend" in params:
            parts = [(part["voice_id"], float(part["weight"])) for part in params["blend"]]
            return blend_from_pack(self._voices_path, parts)
        return params["voice_id"]

    def synthesize_described(
        self,
        text: str,
        params: dict,
        *,
        speed: float,
        lang: str,
        phonemes: str | None = None,
        options: OnnxSynthesisOptions | None = None,
    ):
        """Speak one catalog voice and return the legacy ``(audio, rate)`` tuple."""
        audio, rate, _timings = self.synthesize_described_timed(
            text,
            params,
            speed=speed,
            lang=lang,
            phonemes=phonemes,
            options=options,
        )
        return audio, rate

    def synthesize_described_timed(
        self,
        text: str,
        params: dict,
        *,
        speed: float,
        lang: str,
        phonemes: str | None = None,
        options: OnnxSynthesisOptions | None = None,
    ):
        """Speak one catalog voice and retain fork timing records when available."""
        controls = options or OnnxSynthesisOptions.from_env()
        voice = self.voice_for_create(params)
        spoken = phonemes if phonemes is not None else text
        kwargs = {
            "voice": voice,
            "speed": speed,
            "lang": lang,
            "trim": controls.trim,
            "sentence_pause": controls.sentence_pause,
            "clause_pause": controls.clause_pause,
            "continuous": controls.continuous,
        }
        if phonemes is not None:
            kwargs["is_phonemes"] = True
        create_timed = getattr(self._kokoro, "create_timed", None)
        if create_timed is not None:
            audio, rate, timings = create_timed(spoken, **kwargs)
            return audio, rate, list(timings)
        if controls.continuous:
            raise RuntimeError(
                "continuous kokoro-onnx synthesis requires the DarbotLM 0.6.1 runtime"
            )
        kwargs.pop("continuous")
        audio, rate = self._kokoro.create(spoken, **kwargs)
        return audio, rate, []

    def synthesize_with_g2p(
        self,
        text: str,
        params: dict,
        g2p,
        *,
        speed: float,
        lang: str,
        options: OnnxSynthesisOptions | None = None,
    ):
        """Phonemize with Misaki, then call the fork with ``is_phonemes=True``.

        A blend passes the ndarray from ``blend_from_pack``. The pinned fork
        chunks/paces the phonemes; the compatibility fallback preserves the
        510-character boundary.
        """
        import numpy as np

        from gen_audio.voices.runtime import phonemize_with_misaki, split_phonemes

        controls = options or OnnxSynthesisOptions.from_env()
        phonemes = phonemize_with_misaki(text, g2p)
        if getattr(self._kokoro, "create_timed", None) is not None:
            audio, sample_rate, _timings = self.synthesize_described_timed(
                phonemes,
                params,
                speed=speed,
                lang=lang,
                phonemes=phonemes,
                options=controls,
            )
            return audio, sample_rate
        pieces: list[np.ndarray] = []
        sample_rate: int | None = None
        for chunk in split_phonemes(phonemes):
            raw_audio, raw_rate = self.synthesize_described(
                chunk, params, speed=speed, lang=lang, phonemes=chunk
            )
            audio = np.ascontiguousarray(np.asarray(raw_audio, dtype=np.float32)).reshape(-1)
            rate = int(raw_rate)
            if sample_rate is None:
                sample_rate = rate
            pieces.append(audio)
        if sample_rate is None or not pieces:
            raise ValueError("kokoro-onnx produced no audio")
        return np.concatenate(pieces), sample_rate

    async def stream_described(
        self,
        text: str,
        params: dict,
        *,
        speed: float,
        lang: str,
        phonemes: str | None = None,
        options: OnnxSynthesisOptions | None = None,
    ):
        """Yield fork audio chunks without writing a substitute buffer."""
        controls = options or OnnxSynthesisOptions.from_env()
        create_stream = getattr(self._kokoro, "create_stream", None)
        if create_stream is None:
            raise RuntimeError("installed kokoro-onnx does not expose create_stream")
        if controls.continuous:
            raise ValueError("kokoro-onnx streaming does not use continuous window synthesis")
        kwargs = {
            "voice": self.voice_for_create(params),
            "speed": speed,
            "lang": lang,
            "trim": controls.trim,
            "sentence_pause": controls.sentence_pause,
            "clause_pause": controls.clause_pause,
        }
        if phonemes is not None:
            kwargs["is_phonemes"] = True
        async for item in create_stream(
            phonemes if phonemes is not None else text, **kwargs
        ):
            yield item

    async def stream_with_g2p(
        self,
        text: str,
        params: dict,
        g2p,
        *,
        speed: float,
        lang: str,
        options: OnnxSynthesisOptions | None = None,
        library: str | Path | None = None,
    ):
        """Phonemize once, then yield DarbotLM Kokoro-ONNX stream chunks."""
        from gen_audio.voices.runtime import phonemize_with_misaki

        phonemes = phonemize_with_misaki(text, g2p, library=library)
        async for item in self.stream_described(
            text,
            params,
            speed=speed,
            lang=lang,
            phonemes=phonemes,
            options=options,
        ):
            yield item

    def synthesize_turns(
        self,
        turns: list[Turn],
        cast: CastMap,
        *,
        gap_s: float = DEFAULT_TURN_GAP_S,
        improve_publish: bool = False,
        options: OnnxSynthesisOptions | None = None,
        library: str | Path | None = None,
    ) -> SynthResult:
        """Synthesize each turn and concatenate with silence between turns.

        ``improve_publish`` runs the 24 kHz publish chain on the concatenation.
        It is off by default so a before/after spectrogram can use the raw render.
        """
        if cast.engine != "kokoro_onnx":
            raise ValueError(
                f"cast map engine is {cast.engine!r}; synth_kokoro_onnx requires 'kokoro_onnx'"
            )
        if not isfinite(gap_s) or gap_s < 0.0:
            raise ValueError("gap_s must be a finite number >= 0")
        if not turns:
            raise ValueError("no turns to synthesize")

        with _MODEL_LOCK:
            return self._synthesize_locked(
                turns,
                cast,
                gap_s=gap_s,
                improve_publish=improve_publish,
                options=options or OnnxSynthesisOptions.from_env(),
                library=library,
            )

    def _synthesize_locked(
        self,
        turns: list[Turn],
        cast: CastMap,
        *,
        gap_s: float,
        improve_publish: bool,
        options: OnnxSynthesisOptions,
        library: str | Path | None,
    ) -> SynthResult:
        speakers, index_of = roster(turns, cast)
        pieces: list[tuple[int, np.ndarray]] = []
        rendered: list[SynthTurn] = []
        sample_rate: int | None = None
        for turn in turns:
            assignment = resolve_turn(turn, cast)
            framework = assignment_framework(assignment, cast)
            if framework != "kokoro_onnx":
                raise ValueError(
                    f"speaker {turn.speaker_id} framework is {framework!r}; "
                    "synth_kokoro_onnx requires 'kokoro_onnx'"
                )
            from gen_audio.voices.agents import agent_g2p, agent_voice

            agent = agent_voice(assignment.voice)
            try:
                from gen_audio.voices.runtime import (
                    resolve_g2p_descriptor,
                    resolve_voice_descriptor,
                )

                described_voice = resolve_voice_descriptor(assignment.voice_id)
                voice_params = (
                    dict(described_voice.params)
                    if described_voice is not None
                    else {"voice_id": assignment.voice}
                )
                phonemes = None
                if options.use_misaki:
                    from gen_audio.voices.runtime import phonemize_with_misaki

                    config_id = (
                        described_voice.g2p
                        if described_voice is not None
                        else _MISAKI_G2P_BY_LANG.get(assignment.lang)
                    )
                    if not config_id:
                        raise ValueError(
                            f"no Misaki config for kokoro-onnx language {assignment.lang}"
                        )
                    phonemes = phonemize_with_misaki(
                        turn.text,
                        resolve_g2p_descriptor(config_id),
                        library=library,
                    )
                raw_audio, raw_rate, timing_rows = self.synthesize_described_timed(
                    turn.text,
                    voice_params,
                    speed=assignment.speed,
                    lang=assignment.lang,
                    phonemes=phonemes,
                    options=options,
                )            except AssertionError as exc:
                raise ValueError(
                    f"kokoro-onnx rejected speaker {turn.speaker_id} voice {assignment.voice!r}: {exc}"
                ) from exc
            audio = np.ascontiguousarray(np.asarray(raw_audio, dtype=np.float32))
            rate = int(raw_rate)
            if sample_rate is None:
                sample_rate = rate
            elif rate != sample_rate:
                audio = resample_audio(audio, rate, sample_rate).astype(np.float32)
                rate = sample_rate
            if audio.ndim != 1:
                raise ValueError(f"expected mono samples from kokoro-onnx, got shape {audio.shape}")
            pieces.append((index_of[turn.speaker_id], audio))
            rendered.append(
                SynthTurn(
                    speaker_id=turn.speaker_id,
                    name=assignment.name,
                    voice=assignment.voice,
                    lang=assignment.lang,
                    speed=assignment.speed,
                    text=turn.text,
                    samples=int(audio.size),
                    sample_rate=rate,
                    timings=[
                        {
                            "phoneme": str(item.phoneme),
                            "start_s": float(item.start),
                            "end_s": float(item.end),
                        }
                        for item in timing_rows
                    ],
                )
            )
        if sample_rate is None or not pieces:
            raise ValueError("kokoro-onnx produced no audio")
        gap = np.zeros(int(round(sample_rate * gap_s)), dtype=np.float32) if gap_s > 0.0 else np.zeros(0, dtype=np.float32)
        mixed, segments = concatenate_turns(pieces, sample_rate, gap)
        phoneme_timings = _absolute_timings(rendered, segments)
        improved_flag = False
        if improve_publish:
            raw_segments = segments
            published = improve(mixed, sample_rate)
            segments = remap_through_improve(
                segments,
                trim_start=published.trim_start,
                trimmed_len=published.trimmed_samples,
                published_len=int(published.audio.size),
            )
            mixed = published.audio
            sample_rate = published.sample_rate
            improved_flag = True
            phoneme_timings = _published_timings(
                rendered,
                raw_segments,
                trim_start=published.trim_start,
                source_len=published.trimmed_samples,
                destination_len=int(published.audio.size),
            )
        return SynthResult(
            audio=mixed,
            sample_rate=sample_rate,
            turns=rendered,
            gap_s=float(gap_s),
            improved=improved_flag,
            speakers=speakers,
            segments=segments,
            phoneme_timings=phoneme_timings,
            providers=self.providers,
        )


def _absolute_timings(rendered: list[SynthTurn], segments: list[SpeechSegment]) -> list[dict]:
    rows: list[dict] = []
    for turn, segment in zip(rendered, segments, strict=True):
        offset = int(segment.start_sample) / float(turn.sample_rate)
        for item in turn.timings:
            rows.append(
                {
                    "speaker_id": turn.speaker_id,
                    "phoneme": item["phoneme"],
                    "start_s": offset + float(item["start_s"]),
                    "end_s": offset + float(item["end_s"]),
                }
            )
    return rows


def _published_timings(
    rendered: list[SynthTurn],
    segments: list[SpeechSegment],
    *,
    trim_start: int,
    source_len: int,
    destination_len: int,
) -> list[dict]:
    if source_len <= 0:
        return []
    rows: list[dict] = []
    scale = float(destination_len) / float(source_len)
    for turn, segment in zip(rendered, segments, strict=True):
        for item in turn.timings:
            start_sample = (
                int(segment.start_sample)
                + round(float(item["start_s"]) * turn.sample_rate)
            )
            end_sample = (
                int(segment.start_sample)
                + round(float(item["end_s"]) * turn.sample_rate)
            )
            start = max(0, start_sample - int(trim_start))
            end = min(source_len, end_sample - int(trim_start))
            if end <= start:
                continue
            rows.append(
                {
                    "speaker_id": turn.speaker_id,
                    "phoneme": item["phoneme"],
                    "start_s": (start * scale) / 24000.0,
                    "end_s": (end * scale) / 24000.0,
                }
            )
    return rows
