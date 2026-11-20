"""PersonaPlex / LiveKit session documents, and the transport behind them.

``build_session`` returns a plan. It does not connect. ``StubLiveKitTransport``
is the default transport: it reports which credentials and runtimes are
missing and refuses to open a room. Credential values are never copied into
the plan or the probe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gen_audio.shows.agent_yankers.errors import AgentYankersError, ConsentRefusal, IntegrationUnavailable
from gen_audio.shows.agent_yankers.models import Mark, MarksIndex, Premise, consent_gaps
from gen_audio.shows.agent_yankers.schema import load_schema, validate_instance

LIVEKIT_URL_ENV = "LIVEKIT_URL"
LIVEKIT_API_KEY_ENV = "LIVEKIT_API_KEY"
LIVEKIT_API_SECRET_ENV = "LIVEKIT_API_SECRET"
PERSONAPLEX_TOKEN_ENV = "PERSONAPLEX_HF_TOKEN"
_CREDENTIAL_ENVS = (
    LIVEKIT_URL_ENV,
    LIVEKIT_API_KEY_ENV,
    LIVEKIT_API_SECRET_ENV,
    PERSONAPLEX_TOKEN_ENV,
)
_PROMPT_LIMIT = 2000


@dataclass(frozen=True)
class TransportProbe:
    integration: str
    runnable: bool
    missing: tuple[str, ...]
    credentials_present: bool

    def as_dict(self) -> dict:
        return {
            "integration": self.integration,
            "runnable": self.runnable,
            "missing": list(self.missing),
            "credentials_present": self.credentials_present,
            "audio_written": False,
        }


class CallTransport(Protocol):
    """Places one already-consented session. Implementations must not invent audio."""

    def probe(self) -> TransportProbe:
        """Report whether this process can open a LiveKit room."""

    def open_session(self, session: dict) -> None:
        """Open the room described by ``session``. Raise when it cannot."""


class StubLiveKitTransport:
    """Does not import LiveKit and does not load PersonaPlex.

    ``open_session`` always raises ``IntegrationUnavailable`` and writes nothing.
    A process that has the env vars set is still not runnable: this package has
    no LiveKit agents SDK and no PersonaPlex runtime.
    """

    integration = "livekit"

    def probe(self) -> TransportProbe:
        missing = [name + " is unset" for name in _CREDENTIAL_ENVS if not _env_set(name)]
        missing.append("livekit agents SDK is not wired in this package")
        missing.append(
            "PersonaPlex runtime is not registered in this package "
            "(nvidia/personaplex-7b-v1 is gated and its weights are not bundled)"
        )
        return TransportProbe(
            integration=self.integration,
            runnable=False,
            missing=tuple(missing),
            credentials_present=not any(item.endswith(" is unset") for item in missing),
        )

    def open_session(self, session: dict) -> None:
        probe = self.probe()
        raise IntegrationUnavailable(self.integration, list(probe.missing))


def build_session(premise: Premise, mark: Mark, records_root: Path) -> dict:
    """Session plan for one mark. Raises ``ConsentRefusal`` before any transport call."""
    gaps = consent_gaps(mark, records_root)
    if gaps:
        raise ConsentRefusal([(mark.id, gaps)])
    if mark.id not in premise.targets:
        raise AgentYankersError(
            f"{mark.id} is not a target of {premise.id}",
            code="premise",
        )
    text_prompt = _text_prompt(premise, mark)
    session = {
        "schema_version": "1.0.0",
        "show": "agent_yankers",
        "premise_id": premise.id,
        "mark_id": mark.id,
        "is_agent": mark.is_agent,
        "consent_record_ref": mark.consent.record_ref,
        "transport": "livekit",
        "speech": "personaplex",
        "improvise": True,
        "credentials": {
            "livekit_url_env": LIVEKIT_URL_ENV,
            "livekit_api_key_env": LIVEKIT_API_KEY_ENV,
            "livekit_api_secret_env": LIVEKIT_API_SECRET_ENV,
            "personaplex_token_env": PERSONAPLEX_TOKEN_ENV,
        },
        "personaplex": {
            "model_id": "nvidia/personaplex-7b-v1",
            "gated": True,
            "voice_code": premise.caller.voice_code,
            "voice_prompt": premise.caller.voice_prompt,
            "text_prompt": text_prompt,
            "sample_rate": 24000,
        },
        "livekit": {
            "room": f"agent-yankers-{premise.id}-{mark.id}",
            "caller_identity": premise.caller.identity,
            "mark_identity": mark.identity,
        },
        "recording": {
            "enabled": True,
            "requires_recorded_consent": True,
            "format": "wav",
            "sample_rate": 24000,
        },
    }
    validate_instance(session, load_schema("session"))
    return session


def sessions_for(premise: Premise, index: MarksIndex, records_root: Path) -> list[dict]:
    """One session per premise target. Any refused mark refuses the whole list.

    Nothing is returned for the marks that did consent when a sibling target
    did not. The caller places every target or none of them.
    """
    selected: list[Mark] = []
    refusals: list[tuple[str, list[str]]] = []
    for mark_id in premise.targets:
        mark = index.get(mark_id)
        gaps = consent_gaps(mark, records_root)
        if gaps:
            refusals.append((mark.id, gaps))
        else:
            selected.append(mark)
    if refusals:
        raise ConsentRefusal(refusals)
    return [build_session(premise, mark, records_root) for mark in selected]


def _text_prompt(premise: Premise, mark: Mark) -> str:
    kind = "a fictional agent" if mark.is_agent else "a consented participant"
    lines = [
        premise.caller.persona_prompt,
        "",
        "Premise outline. Improvise from it. Do not read it as a script.",
    ]
    lines.extend(f"{index}. {item}" for index, item in enumerate(premise.outline, start=1))
    lines.append("")
    lines.append(
        f"The mark's name is {mark.display_name}. They are {kind}. "
        "Use your own agent name. Stop if they withdraw consent."
    )
    text = "\n".join(lines)
    if len(text) > _PROMPT_LIMIT:
        raise AgentYankersError("PersonaPlex text prompt exceeds 2000 characters", code="prompt")
    return text


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())
