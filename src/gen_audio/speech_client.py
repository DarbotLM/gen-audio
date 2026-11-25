"""Client for dlm_speech, with explicit decoding at the audio boundary."""

import asyncio
import io
import json
import os
from http.client import IncompleteRead
from urllib.parse import urlsplit, urlunsplit
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


def provider_status(name: str) -> dict:
    if not name or name not in {"vibevoice", "personaplex"}:
        raise ValueError(f"unknown DLM Speech provider: {name}")
    request = Request(speech_base_url() + f"/v1/providers/{name}")
    _authorize(request)
    with urlopen(request, timeout=3) as response:
        metadata = json.loads(response.read(32768))
    provider = metadata.get("provider", {}) if isinstance(metadata, dict) else {}
    if not isinstance(provider, dict):
        raise ValueError(  # noqa: TRY004
            f"DLM Speech returned invalid {name} provider metadata"
        )
    return provider


def check_vibevoice_provider() -> None:
    provider = provider_status("vibevoice")
    if not provider.get("enabled") or not provider.get("configured"):
        raise ValueError("DLM Speech has no configured VibeVoice Realtime provider")


def check_personaplex_provider() -> None:
    provider = provider_status("personaplex")
    if not provider.get("enabled") or provider.get("state") not in {"configured", "ready"}:
        raise ValueError("DLM Speech has no enabled PersonaPlex provider")


def dlm_speech_status() -> dict:
    request = Request(speech_base_url() + "/readyz")
    _authorize(request)
    with urlopen(request, timeout=5) as response:
        payload = json.loads(response.read(131072))
    if not isinstance(payload, dict):
        raise ValueError("DLM Speech readiness returned invalid JSON")  # noqa: TRY004
    return payload


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


class PersonaPlexRealtimeSession:
    """One full-duplex DLM Speech /v1/realtime/personaplex session."""

    def __init__(
        self,
        *,
        voice: str,
        text_prompt: str = "",
        seed: int | None = None,
        audio_temperature: float = 0.8,
        text_temperature: float = 0.7,
        audio_top_k: int = 250,
        text_top_k: int = 25,
    ) -> None:
        self.start = {
            "version": 1,
            "type": "session.start",
            "voice": voice,
            "text_prompt": text_prompt,
            "seed": seed,
            "audio_temperature": audio_temperature,
            "text_temperature": text_temperature,
            "audio_top_k": audio_top_k,
            "text_top_k": text_top_k,
        }
        self._http = None
        self._socket = None

    async def __aenter__(self):
        try:
            import aiohttp
        except ImportError as exc:
            raise ValueError(
                "PersonaPlex realtime needs aiohttp; install runtimes/requirements-dlm-speech.txt "
                "or use the isolated DLM Speech environment"
            ) from exc

        parts = urlsplit(speech_base_url())
        scheme = "wss" if parts.scheme == "https" else "ws"
        url = urlunsplit((scheme, parts.netloc, "/v1/realtime/personaplex", "", ""))
        headers = {}
        token = os.environ.get("GEN_AUDIO_DLM_SPEECH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = aiohttp.ClientSession()
        self._socket = await self._http.ws_connect(
            url,
            headers=headers,
            heartbeat=30,
            max_msg_size=1024 * 1024,
        )
        await self._socket.send_json(self.start)
        message = await self._socket.receive(timeout=300)
        if message.type != aiohttp.WSMsgType.TEXT:
            await self._close()
            raise ValueError("DLM Speech did not send a session.start receipt")
        payload = json.loads(message.data)
        if payload.get("type") == "error":
            await self._close()
            detail = payload.get("error", {}).get("message") or "session refused"
            raise ValueError(f"DLM Speech PersonaPlex session failed: {detail}")
        if payload.get("type") != "session.started":
            await self._close()
            raise ValueError("DLM Speech returned an unexpected PersonaPlex receipt")
        return self

    async def __aexit__(self, *_exc):
        await self._close()

    async def _close(self) -> None:
        if self._socket is not None and not self._socket.closed:
            await self._socket.close()
        if self._http is not None and not self._http.closed:
            await self._http.close()

    async def send_pcm16(self, pcm: bytes, *, timeout: float = 120) -> None:
        if self._socket is None or self._socket.closed:
            raise ValueError("PersonaPlex realtime session is closed")
        if not pcm or len(pcm) % 2:
            raise ValueError("PCM16 input must contain whole nonempty samples")
        await asyncio.wait_for(self._socket.send_bytes(pcm), timeout)

    async def receive(self, *, timeout: float = 300):
        try:
            import aiohttp
        except ImportError as exc:
            raise ValueError("PersonaPlex realtime requires aiohttp") from exc

        if self._socket is None or self._socket.closed:
            raise ValueError("PersonaPlex realtime session is closed")
        message = await asyncio.wait_for(self._socket.receive(), timeout)
        if message.type == aiohttp.WSMsgType.BINARY:
            return message.data
        if message.type == aiohttp.WSMsgType.TEXT:
            return json.loads(message.data)
        if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
            raise EOFError("DLM Speech PersonaPlex session closed")
        raise ValueError(f"DLM Speech returned unexpected WebSocket frame {message.type}")

    async def cancel(self) -> None:
        if self._socket is not None and not self._socket.closed:
            await self._socket.send_json({"version": 1, "type": "session.cancel"})
