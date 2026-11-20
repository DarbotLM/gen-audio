"""Call runner.

Consent is decided before a transport is asked anything. A mark without
recorded consent from both the caller and the mark is refused, and so is
every other target in that premise: the runner places the whole list or
nothing. ``is_agent`` does not waive the check.
"""

from __future__ import annotations

from pathlib import Path

from gen_audio.shows.agent_yankers.errors import IntegrationUnavailable
from gen_audio.shows.agent_yankers.models import MarksIndex, Premise
from gen_audio.shows.agent_yankers.session import CallTransport, StubLiveKitTransport, sessions_for


class CallRunner:
    """Plans and places premise targets through a ``CallTransport``.

    The default transport is the LiveKit stub. It never opens a room.
    """

    def __init__(self, records_root: Path, transport: CallTransport | None = None):
        self.records_root = records_root
        self.transport = transport if transport is not None else StubLiveKitTransport()

    def sessions(self, premise: Premise, index: MarksIndex) -> list[dict]:
        """Return session plans. Raises ``ConsentRefusal`` without calling the transport."""
        return sessions_for(premise, index, self.records_root)

    def place(self, premise: Premise, index: MarksIndex) -> None:
        """Open one session per target. Consent failures never reach ``probe`` or ``open_session``."""
        planned = self.sessions(premise, index)
        probe = self.transport.probe()
        if not probe.runnable:
            raise IntegrationUnavailable(probe.integration, list(probe.missing))
        for session in planned:
            self.transport.open_session(session)
