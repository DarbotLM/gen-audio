"""VibeVoice host lines and the show's laughter and stinger beds.

Host cues would be spoken by VibeVoice. This package does not call that
runtime, so the stub refuses and writes no file. Laughter and stingers are
show beds: when ``GEN_AUDIO_AGENT_YANKERS_SFX_DIR`` contains ``<sfx_id>.wav``
the stub copies that file. It does not synthesize a laugh or a stinger.
"""

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


class StubVibeVoiceShowAudio:
    """Host lines stay unwired. SFX beds are copied only when the file is already there."""

    integration = "vibevoice"

    def probe(self) -> TransportProbe:
        missing = _host_missing()
        sfx = os.environ.get(SFX_DIR_ENV, "").strip()
        if not sfx:
            missing.append(f"{SFX_DIR_ENV} is unset")
        elif not Path(sfx).is_dir():
            missing.append(f"{SFX_DIR_ENV} is not a directory")
        return TransportProbe(
            integration=self.integration,
            runnable=False,
            missing=tuple(missing),
            credentials_present=bool(os.environ.get(VIBEVOICE_MODEL_ENV, "").strip()),
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
        raise IntegrationUnavailable(self.integration, _host_missing() + _sfx_missing(cue))


def _host_missing() -> list[str]:
    missing: list[str] = []
    if not _module_present("torch"):
        missing.append("PyTorch is not installed")
    if not _module_present("vibevoice"):
        missing.append("vibevoice package is not installed")
    model = os.environ.get(VIBEVOICE_MODEL_ENV, "").strip()
    if not model:
        missing.append(f"{VIBEVOICE_MODEL_ENV} is unset (expected a local VibeVoice-1.5B directory)")
    elif not Path(model).is_dir():
        missing.append(f"VibeVoice-1.5B weights are not a directory at {VIBEVOICE_MODEL_ENV}")
    missing.append("Agent Yankers host lines are not wired to a VibeVoice render")
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
