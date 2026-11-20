"""Optional import boundary for the private AgentYankers core.

``genaid_audio.agentyankers`` lives in DarbotLM/darbot-vllm. This module imports
it when the host has installed it and otherwise reports that the core is
absent. Nothing here vendors that package or its voice assets.
"""

from __future__ import annotations

import importlib
from typing import Any

from gen_audio.agentyankers.contract import SCHEMA_VERSION, CoreUnavailable

CORE_MODULE = "genaid_audio.agentyankers"

_MISSING = object()
_core: Any = _MISSING


def import_core() -> Any | None:
    """Return the installed core module, or ``None`` when it is not installed."""
    global _core
    if _core is not _MISSING:
        return _core
    try:
        _core = importlib.import_module(CORE_MODULE)
    except ImportError:
        _core = None
    return _core


def reset_core_cache() -> None:
    """Drop the cached import. Tests use this; hosts can call it after install."""
    global _core
    _core = _MISSING


def core_status() -> dict[str, Any]:
    """Whether this process can delegate live calls to the private package."""
    module = import_core()
    if module is None:
        return {
            "available": False,
            "module": CORE_MODULE,
            "schema_version": SCHEMA_VERSION,
        }
    version = getattr(module, "SCHEMA_VERSION", SCHEMA_VERSION)
    return {
        "available": True,
        "module": CORE_MODULE,
        "schema_version": version,
    }


def require_core() -> Any:
    """Return the core module or raise ``CoreUnavailable``."""
    module = import_core()
    if module is None:
        raise CoreUnavailable(
            f"{CORE_MODULE} is not installed. "
            "This package can dry-run an episode from a rundown. "
            "Live calls, CallRunner, and EpisodePipeline.assemble stay in that package."
        )
    return module


def assemble_with_core(
    request: Any,
    bank: Any,
    renderer: Any,
    catalog: Any = None,
) -> Any:
    """Call ``EpisodePipeline.assemble`` on the installed core.

    ``request`` and ``bank`` are core objects (``EpisodeRequest``, ``AudioBank``).
    ``renderer`` is a ``VoiceoverRenderer``; pass ``GenAudioVoiceoverRenderer``
    so host lines use this repo's VibeVoice adapter.
    """
    module = require_core()
    pipeline = module.EpisodePipeline(renderer, catalog)
    return pipeline.assemble(request, bank)


def core_export_json_schemas() -> dict[str, Any] | None:
    """The core's ``export_json_schemas`` when the package is installed."""
    module = import_core()
    if module is None or not hasattr(module, "export_json_schemas"):
        return None
    exported = module.export_json_schemas()
    if isinstance(exported, dict) and exported:
        return exported
    return None
