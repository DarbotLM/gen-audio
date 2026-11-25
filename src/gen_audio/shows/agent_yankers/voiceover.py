"""VibeVoice host lines and the show's laughter and stinger beds."""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gen_audio.shows.agent_yankers.errors import IntegrationUnavailable
from gen_audio.shows.agent_yankers.models import Cue
from gen_audio.shows.agent_yankers.session import TransportProbe

VIBEVOICE_MODEL_ENV = "GEN_AUDIO_VIBEVOICE_MODEL"
SFX_DIR_ENV = "GEN_AUDIO_AGENT_YANKERS_SFX_DIR"


@dataclass(frozen=True)
class RenderResult:
    cue_id: str
    role: str
    source: str
    status: str
    output: str | None
    synthesized_speech: bool

    def as_dict(self) -> dict:
        return {
            "cue_id": self.cue_id,
            "role": self.role,
            "source": self.source,
            "status": self.status,
            "output": self.output,
            "synthesized_speech": self.synthesized_speech,
        }


class ShowAudio(Protocol):
    """Renders one voiceover cue. A refusal must leave ``output`` uncreated."""

    def probe(self) -> TransportProbe:
        """Report whether host lines and SFX beds can be produced."""

    def render(self, cue: Cue, output: Path) -> RenderResult:
        """Write ``output`` or raise ``IntegrationUnavailable`` without creating it."""


class VibeVoiceShowAudio:
    """VibeVoice-backed host renderer with copied, pre-existing SFX beds."""

    integration = "vibevoice"

    def probe(self) -> TransportProbe:
        missing = _host_missing()
        sfx = os.environ.get(SFX_DIR_ENV, "").strip()
        if sfx and not Path(sfx).is_dir():
            missing.append(f"{SFX_DIR_ENV} is not a directory")
        return TransportProbe(
            integration=self.integration,
            runnable=not missing,
            missing=tuple(missing),
            credentials_present=True,
        )

    def render(self, cue: Cue, output: Path) -> RenderResult:
        if cue.role in {"laughter", "stinger"}:
            copied = _copy_sfx(cue, output)
            if copied is not None:
                return copied
        if output.exists():
            raise IntegrationUnavailable(
                self.integration,
                [f"refusing to overwrite {output.name}"],
            )
        if cue.role == "host":
            if not cue.text:
                raise IntegrationUnavailable(self.integration, ["host cue has no text"])
            missing = _host_missing()
            if missing:
                raise IntegrationUnavailable(self.integration, missing)
            from gen_audio.adapters import EngineRefusal, synthesize
            from gen_audio.cast import CastMap, Turn, VoiceAssignment

            voice = os.environ.get("GEN_AUDIO_AGENT_YANKERS_HOST_VOICE", "en-Carter_man").strip()
            assignment = VoiceAssignment(
                "1",
                "Host",
                voice,
                "en-us",
                1.0,
                framework="vibevoice",
                voice_id=f"vibevoice.{voice}",
            )
            cast = CastMap("vibevoice", "en-us", 1.0, {"1": assignment})
            try:
                synthesize("vibevoice", [Turn("1", cue.text, None, 1)], cast, output)
            except EngineRefusal as exc:
                raise IntegrationUnavailable(self.integration, exc.missing) from exc
            return RenderResult(
                cue_id=cue.id,
                role=cue.role,
                source="vibevoice",
                status="rendered",
                output=output.name,
                synthesized_speech=True,
            )
        raise IntegrationUnavailable(self.integration, _host_missing() + _sfx_missing(cue))


def _host_missing() -> list[str]:
    from gen_audio.adapters import probe

    found = probe("vibevoice")
    if found.available:
        return []
    missing = list(found.missing)
    if not os.environ.get("GEN_AUDIO_VIBEVOICE_WORKER_URL", "").strip():
        missing.append("host voiceover is not wired to a VibeVoice render")
    return missing


def _sfx_missing(cue: Cue) -> list[str]:
    if cue.sfx_id is None:
        return []
    sfx = os.environ.get(SFX_DIR_ENV, "").strip()
    if not sfx:
        return [f"{SFX_DIR_ENV} is unset; {cue.sfx_id}.wav was not copied"]
    path = Path(sfx)
    if not path.is_dir() or not (path / f"{cue.sfx_id}.wav").is_file():
        return [f"{cue.sfx_id}.wav is not in {SFX_DIR_ENV}; no substitute bed was synthesized"]
    return []


def _copy_sfx(cue: Cue, output: Path) -> RenderResult | None:
    if cue.sfx_id is None:
        return None
    root = os.environ.get(SFX_DIR_ENV, "").strip()
    if not root:
        return None
    source = Path(root) / f"{cue.sfx_id}.wav"
    if not source.is_file():
        return None
    if output.exists():
        raise IntegrationUnavailable("vibevoice", [f"refusing to overwrite {output.name}"])
    if not output.parent.is_dir():
        raise IntegrationUnavailable("vibevoice", ["output directory does not exist"])
    output.write_bytes(source.read_bytes())
    return RenderResult(
        cue_id=cue.id,
        role=cue.role,
        source="sfx",
        status="rendered",
        output=output.name,
        synthesized_speech=False,
    )


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


StubVibeVoiceShowAudio = VibeVoiceShowAudio
