"""Client for dlm_speech, with explicit decoding at the audio boundary."""

import io
import json
import os
from http.client import IncompleteRead
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import numpy as np
import soundfile as sf

from gen_audio.audio_io import as_mono

_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def speech_base_url() -> str:
    base = os.environ.get("GEN_AUDIO_DLM_SPEECH_URL", "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.hostname or any(
        (parts.username, parts.password, parts.query, parts.fragment)
    ):
        raise ValueError("set GEN_AUDIO_DLM_SPEECH_URL to the dlm_speech HTTP(S) base URL")
    return base


def decode_audio(payload: bytes, headers) -> tuple[np.ndarray, int]:
    media_type = headers.get_content_type()
    if media_type in {"audio/wav", "audio/x-wav", "audio/wave", "audio/flac", "audio/mpeg", "audio/ogg"}:
        samples, rate = sf.read(io.BytesIO(payload), dtype="float32", always_2d=True)
        return as_mono(samples), rate
    if media_type not in {"audio/l16", "application/octet-stream"}:
        raise ValueError(f"unsupported speech response type: {media_type}")

    # Older dlm_speech builds explicitly label their little-endian stream
    # in X-Audio-Encoding even though their Content-Type was audio/L16.
    encoding = headers.get("X-Audio-Encoding") or ("pcm_s16be" if media_type == "audio/l16" else "")
    dtype = {"pcm_s16le": "<i2", "pcm_s16be": ">i2"}.get(encoding.lower())
    if dtype is None:
        raise ValueError(f"unsupported speech PCM encoding: {encoding}")
    rate = int(headers.get("X-Audio-Sample-Rate") or headers.get_param("rate") or 0)
    channels = int(headers.get("X-Audio-Channels") or headers.get_param("channels") or 1)
    if not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
        raise ValueError("invalid speech PCM sample rate or channel count")
    if not payload or len(payload) % (2 * channels):
        raise ValueError("speech response contains incomplete or empty PCM frames")
    samples = np.frombuffer(payload, dtype=dtype).reshape(-1, channels)
    return as_mono(samples), rate


def _authorize(request: Request) -> None:
    token = os.environ.get("GEN_AUDIO_DLM_SPEECH_TOKEN")
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")


def check_vibevoice_provider() -> None:
    request = Request(speech_base_url() + "/v1/providers/vibevoice")
    _authorize(request)
    with urlopen(request, timeout=3) as response:
        metadata = json.loads(response.read(32768))
    provider = metadata.get("provider", {}) if isinstance(metadata, dict) else {}
    if not isinstance(provider, dict) or not provider.get("enabled") or not provider.get("configured"):
        raise ValueError("DLM Speech has no configured VibeVoice Realtime provider")


def vibevoice_realtime(text: str, voice: str) -> tuple[np.ndarray, int]:
    request = Request(
        speech_base_url() + "/v1/tts/vibevoice",
        data=json.dumps({"version": 1, "text": text, "voice": voice}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "audio/wav, application/octet-stream"},
        method="POST",
    )
    _authorize(request)
    with urlopen(request, timeout=300) as response:
        try:
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
        except IncompleteRead as exc:
            raise ValueError("speech response ended before the audio was complete") from exc
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise ValueError("speech response exceeds the maximum audio size")
        return decode_audio(payload, response.headers)
