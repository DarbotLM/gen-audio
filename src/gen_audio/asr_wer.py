"""Local ASR and word error rate."""

from __future__ import annotations

from functools import lru_cache
import os
import re
import shutil
import subprocess
from pathlib import Path
from threading import Lock

_VIBEVOICE_LOCK = Lock()
_WORD = re.compile(r"[a-z0-9']+")


class AsrError(RuntimeError):
    """ASR could not run. The message names what is missing."""


def normalize_words(text: str) -> list[str]:
    """Lowercase word tokens. Punctuation is not part of the WER alignment."""
    return _WORD.findall(text.lower())


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word error rate in ``[0, inf)``. An empty reference with no hypothesis is 0."""
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(ref, hyp) / float(len(ref))


def recognizer_name() -> str:
    if model_dir := os.environ.get("GEN_AUDIO_VIBEVOICE_ASR_MODEL"):
        return "VibeVoice ASR-HF " + Path(model_dir).name
    return os.environ.get("GEN_AUDIO_NEMOTRON_ASR_MODEL") or "faster-whisper tiny.en"


def transcribe(path: Path | str) -> str:
    """Transcribe a WAV with the configured recognizer."""
    wav = Path(path)
    if not wav.is_file():
        raise AsrError(f"ASR input is missing: {wav.name}")
    if model_dir := os.environ.get("GEN_AUDIO_VIBEVOICE_ASR_MODEL"):
        from gen_audio.audio_io import read_wav

        try:
            audio, rate = read_wav(wav)
            with _VIBEVOICE_LOCK:
                recognizer = _vibevoice_recognizer(Path(model_dir).resolve())
                return recognizer.transcribe(audio, rate, context=os.environ.get("GEN_AUDIO_VIBEVOICE_ASR_CONTEXT"))["text"]
        except Exception as exc:
            raise AsrError(f"VibeVoice ASR failed: {exc}") from exc
    model_id = os.environ.get("GEN_AUDIO_NEMOTRON_ASR_MODEL")
    if model_id:
        return run_nemotron(wav, model_id, encoder=os.environ.get("GEN_AUDIO_NEMOTRON_ASR_ENCODER"))
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AsrError("faster-whisper is not installed") from exc
    try:
        model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(wav), language="en", vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments).strip()
    except Exception as exc:
        raise AsrError(f"faster-whisper failed: {exc}") from exc
    return text


@lru_cache(maxsize=1)
def _vibevoice_recognizer(directory: Path):
    from gen_audio.vibevoice_asr import VibeVoiceASR

    return VibeVoiceASR(directory)


def run_nemotron(
    wav: Path, model: str, *, encoder: str | None = None, language: str | None = None,
    timed: bool = False, timeout: int = 300,
) -> str:
    """Run the native recognizer, optionally with its separate SALM encoder."""
    binary = shutil.which(os.environ.get("GEN_AUDIO_NEMO_SPEECH") or "nemo-speech")
    if not binary:
        raise AsrError("Nemotron ASR requires the local nemo-speech executable")
    command = [binary, "transcribe", str(wav), "--model", model, "--format", "json" if timed else "text"]
    if encoder:
        command.extend(["--salm-encoder", encoder, "--device", "cpu"])
    if language and language != "auto":
        command.extend(["--language", language])
    if timed:
        command.append("--word-times")
    try:
        result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                                timeout=timeout, check=True)
    except subprocess.CalledProcessError as exc:
        raise AsrError(f"Nemotron ASR exited with code {exc.returncode}: {exc.stderr.strip()[-500:]}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AsrError(f"Nemotron ASR failed: {exc}") from exc
    return result.stdout.strip()


def _levenshtein(ref: list[str], hyp: list[str]) -> int:
    rows = len(ref) + 1
    cols = len(hyp) + 1
    previous = list(range(cols))
    for i in range(1, rows):
        current = [i]
        for j in range(1, cols):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]
