"""Compatibility surface for the native PowerShell plugin host.

VibeVoice and PersonaPlex 7B already dispatch through ``scripts/plugins.ps1``.
The remaining Python engines stay isolated here until their native plugins are
implemented; callers retain the existing ``probe`` and ``synthesize`` API.
"""

from gen_audio.legacy_adapters import EngineRefusal, Probe, probe, synthesize

__all__ = ["EngineRefusal", "Probe", "probe", "synthesize"]
