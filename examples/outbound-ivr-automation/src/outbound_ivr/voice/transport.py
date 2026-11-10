"""Per-call transport: audio in/out, DTMF out, hang-up, and mode/case lookup.

`outbound_ivr.voice.agent` runs the same Strands `BidiAgent` over a `Transport`; the one
implementation used today, `LiveKitTransport` (`outbound_ivr.voice.livekit_transport`),
carries a LiveKit room's audio in/out, DTMF, and hang-up behind this interface, so
`agent.py`'s phase runner, audio streams, and tools depend only on this abstract
contract, never on LiveKit directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from outbound_ivr.voice.frames import Frame


class TransportError(Exception):
    """A transport-level failure: a bad handshake, or the connection lost mid-frame."""


# --------------------------------------------------------------------------
# Transport interface
# --------------------------------------------------------------------------


class Transport(ABC):
    """Everything a call's audio in/out, DTMF out, hang-up, and mode/case lookup need.

    `agent.py`'s phase runner, audio streams, and tools depend only on this interface,
    never on the wire underneath.
    """

    call_uuid: str
    mode: str
    case_id: str | None

    @property
    @abstractmethod
    def peer(self) -> str:
        """A short description of who's on the other end, for logging."""

    @abstractmethod
    async def handshake(self) -> str:
        """Perform the connection's opening exchange; return the call UUID.

        Also sets `self.mode` (`full_call` or `intake`) and `self.case_id`.
        """

    @abstractmethod
    def frames(self) -> AsyncIterator[Frame]:
        """Yield inbound frames (AUDIO, DTMF, ERROR) until HANGUP or the peer disconnects."""

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Send one chunk of outbound 8 kHz 16-bit mono PCM."""

    @abstractmethod
    async def press_key(self, digit: str) -> None:
        """Press one DTMF key on the call."""

    @abstractmethod
    async def hang_up(self) -> None:
        """End the call from the agent's side."""

    @abstractmethod
    async def wait_for_team_join(self, timeout: float) -> str | None:
        """Wait up to `timeout` seconds for a colleague to join the call; return their
        identity, or `None` if nobody joined in time (`transfer_to_team`)."""

    def send_event(self, record: dict[str, Any]) -> None:
        """Forward one `EventLog` record upstream. No-op unless a transport overrides it."""

    @abstractmethod
    async def close(self) -> None:
        """Release the underlying connection."""
