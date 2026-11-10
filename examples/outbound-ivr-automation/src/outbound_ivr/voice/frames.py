"""The call's audio frame vocabulary, shared by every Transport implementation.

A `Frame` is one parsed audio/DTMF/error message flowing between a Transport and
`outbound_ivr.voice.agent`: `AUDIO_FRAME_BYTES` (320, 8 kHz 16-bit mono PCM, 20 ms) is the
agent's fixed frame size for both directions, and `MsgType`/`FrameError` are the small
closed vocabulary `Transport.frames()` implementations yield and `agent.read_audio`
switches on. `LiveKitTransport` (the deployed transport) constructs these same
objects directly; this module has no transport-specific code of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

AUDIO_FRAME_BYTES = 320  # 8 kHz, 16-bit mono, 20 ms


class MsgType(IntEnum):
    """A frame's message type."""

    HANGUP = 0x00
    UUID = 0x01
    DTMF = 0x03
    AUDIO = 0x10
    ERROR = 0xFF


class FrameError(Exception):
    """A malformed frame, or the connection closed mid-frame."""


@dataclass
class Frame:
    """One parsed audio/DTMF/error message."""

    type: int
    payload: bytes = b""
