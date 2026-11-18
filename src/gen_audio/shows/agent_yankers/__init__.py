"""Agent Yankers: premise, consented marks, a call plan, and a genaid episode manifest.

Importing this package does not open LiveKit, load PersonaPlex, or synthesize speech.
"""

from gen_audio.shows.agent_yankers.errors import (
    AgentYankersError,
    ConsentRefusal,
    IntegrationUnavailable,
    SchemaError,
)
from gen_audio.shows.agent_yankers.manifest import assemble_episode, check_episode, highlight_reel, load_episode
from gen_audio.shows.agent_yankers.models import (
    load_highlights,
    load_marks,
    load_premise,
    load_voiceover,
)
from gen_audio.shows.agent_yankers.runner import CallRunner
from gen_audio.shows.agent_yankers.session import StubLiveKitTransport, build_session
from gen_audio.shows.agent_yankers.voiceover import StubVibeVoiceShowAudio

__all__ = [
    "AgentYankersError",
    "CallRunner",
    "ConsentRefusal",
    "IntegrationUnavailable",
    "SchemaError",
    "StubLiveKitTransport",
    "StubVibeVoiceShowAudio",
    "assemble_episode",
    "build_session",
    "check_episode",
    "highlight_reel",
    "load_episode",
    "load_highlights",
    "load_marks",
    "load_premise",
    "load_voiceover",
]
