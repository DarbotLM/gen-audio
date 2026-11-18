"""Published AgentYankers consent and contact rules.

Live calls stay in ``genaid_audio.agentyankers`` when that package is installed.
These checks run in this public SDK before a rundown is mixed, so a missing
opt-in stops the episode with or without the private core.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


SHOW_NAME = "AgentYankers"
PRODUCER = "DarbotLabs"
SCHEMA_VERSION = "agentyankers/v1"
SAMPLE_RATE = 24_000

RECORDING_DISCLOSURE = (
    "This is AgentYankers, an adult comedy podcast from DarbotLabs. "
    "This call is being recorded. You are on with an automated agent. "
    "Say stop if you want the call to end."
)

ENV_LIVEKIT_URL = "LIVEKIT_URL"
ENV_LIVEKIT_API_KEY = "LIVEKIT_API_KEY"
ENV_LIVEKIT_API_SECRET = "LIVEKIT_API_SECRET"
ENV_PERSONAPLEX_MODEL = "GENAID_PERSONAPLEX_MODEL_BASE"
ENV_VIBEVOICE_MODEL = "GENAID_MODEL_BASE"
ENV_VOICES_DIR = "GENAID_VOICES_DIR"
DOCUMENTED_VOICE_ROOT = "/mnt/fschia/genai-media/audio/voice-library"

_PHONE_PATTERNS = (
    re.compile(r"\+\d[\d\s().-]{7,}\d"),
    re.compile(r"\(\d{3}\)\s*\d{3}[-.\s]\d{4}"),
    re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),
    re.compile(r"\b\d{10,15}\b"),
)

_SECRET_KEYS = {
    "livekit_api_key",
    "livekit_api_secret",
    "api_key",
    "api_secret",
    "token",
    "password",
    "authorization",
}


class AgentYankersError(ValueError):
    """Base error for the AgentYankers contract. ``code`` is stable for callers."""

    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConsentRefusal(AgentYankersError):
    """The pipeline will not call or publish this mark."""

    def __init__(self, message: str, *, mark_id: str, action: str, code: str):
        super().__init__(message)
        self.mark_id = mark_id
        self.action = action
        self.code = code


class PhoneNumberRefusal(AgentYankersError):
    """A phone-number-like string appeared. The matched text is not echoed."""

    code = "phone_number_forbidden"

    def __init__(self, field: str):
        self.field = field
        super().__init__(
            f"Refusing {field}: AgentYankers does not accept phone numbers. "
            "Use an agent id or a LiveKit identity."
        )


class CredentialsMissing(AgentYankersError):
    """A live adapter was asked to run without the env vars it needs."""

    code = "credentials_missing"

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        names = ", ".join(self.missing)
        super().__init__(
            f"Live credentials are not configured. Set environment variables: {names}."
        )


class CoreUnavailable(AgentYankersError):
    """The private AgentYankers package is not importable in this process."""

    code = "core_unavailable"


class VibeVoiceRuntimeUnavailable(AgentYankersError):
    """Render mode needs the local VibeVoice adapter and weights."""

    code = "vibevoice_unavailable"


class MissingRecording(AgentYankersError):
    code = "missing_recording"


def recording_disclosure() -> str:
    """The exact sentence inserted into every call and every episode."""
    return RECORDING_DISCLOSURE


def find_phone_numbers(value: Any) -> list[str]:
    """Return matched phone-like strings. Callers should not log the result."""
    found: list[str] = []
    _walk_phones(value, found)
    return found


def assert_no_phone_numbers(value: Any, *, field: str = "payload") -> None:
    if find_phone_numbers(value):
        raise PhoneNumberRefusal(field)


def reject_inline_secrets(value: Any) -> None:
    """Refuse secret-shaped keys. Values stay in the environment, not the body."""
    _walk_secrets(value)


def explain_consent(mark: Any) -> str:
    name = str(_field(mark, "display_name"))
    consent = _field(mark, "consent")
    opted = bool(_field(mark, "opted_in")) and bool(_field(consent, "opted_in"))
    if not opted:
        return f"{name} has not opted in"
    scope = str(_field(consent, "scope"))
    if scope == "call":
        return f"{name} opted in for a call only; publishing is refused"
    if scope == "publish":
        return f"{name} opted in for publication only; a new call is refused"
    if scope == "call_and_publish":
        return f"{name} opted in for calls and publication"
    return f"{name} has no usable consent scope"


def require_call_consent(mark: Any) -> None:
    _require(mark, action="call", scopes={"call", "call_and_publish"})


def require_publish_consent(mark: Any) -> None:
    _require(mark, action="publish", scopes={"publish", "call_and_publish"})


def _require(mark: Any, *, action: str, scopes: set[str]) -> None:
    consent = _field(mark, "consent")
    mark_id = str(_field(mark, "mark_id"))
    name = str(_field(mark, "display_name"))
    if not bool(_field(mark, "opted_in")) or not bool(_field(consent, "opted_in")):
        verb = "call" if action == "call" else "publish"
        raise ConsentRefusal(
            f"{name} has not opted in. AgentYankers will not {verb} this mark.",
            mark_id=mark_id,
            action=action,
            code="consent_missing",
        )
    if str(_field(consent, "scope")) not in scopes:
        if action == "publish":
            message = f"{explain_consent(mark)}."
        else:
            message = f"{name} is not consented for a call."
        raise ConsentRefusal(message, mark_id=mark_id, action=action, code="consent_scope")
    if not bool(_field(consent, "disclosure_accepted")):
        raise ConsentRefusal(
            f"{name} has not accepted the recording disclosure.",
            mark_id=mark_id,
            action=action,
            code="disclosure_required",
        )


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        try:
            return value[name]
        except KeyError as exc:
            raise KeyError(name) from exc
    return getattr(value, name)


def _walk_phones(value: Any, found: list[str]) -> None:
    if isinstance(value, str):
        for pattern in _PHONE_PATTERNS:
            found.extend(pattern.findall(value))
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _walk_phones(item, found)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_phones(item, found)


def _walk_secrets(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                lowered = key.strip().lower()
                if lowered in _SECRET_KEYS or lowered.endswith("_secret") or lowered.endswith("_token"):
                    raise AgentYankersError(
                        "Refusing payload: secrets belong in environment variables, not the request."
                    )
            _walk_secrets(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_secrets(item)
