"""`_wait_for_handoff_to_end`: the backstop for a handed-off call going silent.

After a warm transfer Outbound IVR stays in the room and waits for the call to end. That wait
used to be a bare `state.hangup.wait()`, which is only ever set once the far end's audio
frames stop arriving, which is delegated entirely to livekit-sip's own no-RTP watchdog.
On a measured real call that watchdog took 120 seconds from the front desk actually
hanging up, cross-confirmed against the Amazon Connect contact record, the livekit-sip
container log ("triggering media timeout", sinceLast 2m0.0006s) and the events table,
while a human on the Evidence leg sat on a dead line.

The deployed LiveKit config claims a 10 second media timeout and the per-call override
claims 120; the log proves 120 was in effect, so neither knob is trustworthy. Hence an
independent, application-level ceiling.

The first version of this watched for frames to STOP ARRIVING, and it never fired on a
real call: livekit-sip keeps publishing an audio track into the room after the remote
stops sending RTP, so frames arrived for the full two minutes. The signal is therefore
frames that carry no SOUND, measured by RMS. These tests pin that distinction, because
it is the whole difference between a fix that works and one that passes its tests and
does nothing.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from outbound_ivr.voice import agent as agent_module
from outbound_ivr.voice.agent import CallState, _wait_for_handoff_to_end


class _RecordingLog:
    """Enough of EventLog for this: the silence path writes one status event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, type_: str, **fields) -> None:
        self.events.append((type_, fields))


def _state() -> CallState:
    return CallState(
        call_uuid="00000000-0000-4000-8000-000000000001",
        mode="intake",
        log=_RecordingLog(),
    )


@pytest.mark.asyncio
async def test_a_real_hangup_still_ends_the_call_immediately(monkeypatch):
    monkeypatch.setattr(agent_module, "_HANDOFF_SILENCE_POLL_S", 0.01)
    monkeypatch.setattr(agent_module, "HANDOFF_SILENCE_TIMEOUT_S", 100.0)
    state = _state()

    async def hang_up_shortly():
        await asyncio.sleep(0.02)
        state.hangup.set()

    asyncio.get_running_loop().create_task(hang_up_shortly())
    assert await _wait_for_handoff_to_end(state) == "far end hung up"


@pytest.mark.asyncio
async def test_silence_ends_the_call_without_waiting_for_the_sip_watchdog(monkeypatch):
    monkeypatch.setattr(agent_module, "_HANDOFF_SILENCE_POLL_S", 0.01)
    monkeypatch.setattr(agent_module, "HANDOFF_SILENCE_TIMEOUT_S", 0.05)
    state = _state()
    state.last_far_audio = time.monotonic() - 10.0  # already long silent

    assert await _wait_for_handoff_to_end(state) == "handoff went silent"
    # Recorded, so a stuck handoff is visible in the event stream rather than only
    # inferable from the call's duration.
    assert any(
        kind == "status" and fields.get("value") == "handoff-silence"
        for kind, fields in state.log.events
    )


@pytest.mark.asyncio
async def test_ongoing_conversation_is_not_cut_off(monkeypatch):
    """The timeout keys off the LAST AUDIO FRAME, not time since the handoff.

    On the measured call the three-way conversation genuinely continued for about 19
    seconds after Outbound IVR's warm introduction finished, so a timer started at handoff
    would have cut off a working call.
    """
    monkeypatch.setattr(agent_module, "_HANDOFF_SILENCE_POLL_S", 0.01)
    monkeypatch.setattr(agent_module, "HANDOFF_SILENCE_TIMEOUT_S", 0.08)
    state = _state()
    talking = True

    async def keep_talking():
        while talking:
            state.last_far_audio = time.monotonic()
            await asyncio.sleep(0.01)

    task = asyncio.get_running_loop().create_task(keep_talking())
    with pytest.raises(TimeoutError):
        # Still going after well past the silence window, because audio keeps arriving.
        await asyncio.wait_for(_wait_for_handoff_to_end(state), timeout=0.3)
    talking = False
    task.cancel()


@pytest.mark.asyncio
async def test_silence_after_real_conversation_is_caught(monkeypatch):
    monkeypatch.setattr(agent_module, "_HANDOFF_SILENCE_POLL_S", 0.01)
    monkeypatch.setattr(agent_module, "HANDOFF_SILENCE_TIMEOUT_S", 0.05)
    state = _state()

    async def talk_then_stop():
        for _ in range(3):
            state.last_far_audio = time.monotonic()
            await asyncio.sleep(0.01)

    asyncio.get_running_loop().create_task(talk_then_stop())
    assert await _wait_for_handoff_to_end(state) == "handoff went silent"


def test_read_audio_stamps_every_frame_regardless_of_level_metering():
    """`read_audio` is the only writer, and it must stamp whether or not OUTBOUND_IVR_LEVELS
    is on: the deployed Runtime runs with it off."""
    import inspect

    src = inspect.getsource(agent_module.read_audio)
    stamp = src.index("state.last_far_audio = time.monotonic()")
    levels = src.index("if state.levels:")
    assert stamp < levels, "the stamp must not sit inside the level-metering branch"


# --------------------------------------------------------------------------
# Silence means no sound, not no frames. This is what the first version got
# wrong: it stamped on every arriving frame, and frames never stop arriving.
# --------------------------------------------------------------------------

SILENT_FRAME = b"\x00\x00" * 160  # 320 bytes of digital silence, one 20ms frame
LOUD_FRAME = b"\x00\x40" * 160  # steady tone, well above the noise floor


def test_digital_silence_does_not_count_as_audio(monkeypatch):
    """A frame of zeros must not refresh the liveness stamp. livekit-sip publishes
    exactly this once inbound RTP stops."""
    import audioop

    from outbound_ivr.voice.agent import _SAMPLE_WIDTH, FAR_AUDIO_SILENCE_RMS

    assert audioop.rms(SILENT_FRAME, _SAMPLE_WIDTH) <= FAR_AUDIO_SILENCE_RMS


def test_real_audio_does_count(monkeypatch):
    import audioop

    from outbound_ivr.voice.agent import _SAMPLE_WIDTH, FAR_AUDIO_SILENCE_RMS

    assert audioop.rms(LOUD_FRAME, _SAMPLE_WIDTH) > FAR_AUDIO_SILENCE_RMS


def test_a_phone_line_noise_floor_still_counts_as_alive():
    """The threshold has to sit below a live line's noise floor, or a quiet but
    connected call would be torn down. G.711 dither lands well above 50 RMS."""
    import audioop
    import random

    from outbound_ivr.voice.agent import _SAMPLE_WIDTH, FAR_AUDIO_SILENCE_RMS

    random.seed(7)
    # +/- 300 of 32767, a conservative floor for a connected line
    noise = b"".join(
        int(random.uniform(-300, 300)).to_bytes(2, "little", signed=True) for _ in range(160)
    )
    assert audioop.rms(noise, _SAMPLE_WIDTH) > FAR_AUDIO_SILENCE_RMS


@pytest.mark.asyncio
async def test_frames_that_keep_arriving_silently_still_end_the_call(monkeypatch):
    """The exact real-world case. Frames arrive continuously, all of them silent, and
    the call must still end rather than waiting on the SIP watchdog."""
    from outbound_ivr.voice import frames as asock

    monkeypatch.setattr(agent_module, "_HANDOFF_SILENCE_POLL_S", 0.01)
    monkeypatch.setattr(agent_module, "HANDOFF_SILENCE_TIMEOUT_S", 0.08)
    state = _state()

    class _SilentTransport:
        async def frames(self):
            while True:
                await asyncio.sleep(0.005)
                yield asock.Frame(type=asock.MsgType.AUDIO, payload=SILENT_FRAME)

    reader = asyncio.get_running_loop().create_task(
        agent_module.read_audio(_SilentTransport(), state)
    )
    try:
        assert await _wait_for_handoff_to_end(state) == "handoff went silent"
    finally:
        reader.cancel()


@pytest.mark.asyncio
async def test_frames_carrying_sound_keep_the_call_alive(monkeypatch):
    from outbound_ivr.voice import frames as asock

    monkeypatch.setattr(agent_module, "_HANDOFF_SILENCE_POLL_S", 0.01)
    monkeypatch.setattr(agent_module, "HANDOFF_SILENCE_TIMEOUT_S", 0.08)
    state = _state()

    class _TalkingTransport:
        async def frames(self):
            while True:
                await asyncio.sleep(0.005)
                yield asock.Frame(type=asock.MsgType.AUDIO, payload=LOUD_FRAME)

    reader = asyncio.get_running_loop().create_task(
        agent_module.read_audio(_TalkingTransport(), state)
    )
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_wait_for_handoff_to_end(state), timeout=0.3)
    finally:
        reader.cancel()


# --------------------------------------------------------------------------
# Both legs count, not just the front desk. The team member's audio never
# reaches `state.last_far_audio`: livekit_transport._pump_team_audio feeds it
# to the recorder only, because Outbound IVR's own session has ended by then. So a
# backstop watching the front desk alone cannot tell a front-desk person
# listening quietly from one who hung up -- and three measured calls all ended
# here at 20-27s for that reason, one while the operator believed it was live.
# --------------------------------------------------------------------------


class _TeamTransport:
    """Only the part of the LiveKit transport the backstop reads."""

    def __init__(self, last_team_audio: float = 0.0) -> None:
        self.last_team_audio = last_team_audio


@pytest.mark.asyncio
async def test_a_talking_team_member_keeps_a_quiet_front_desk_alive(monkeypatch):
    """The regression. Front desk silent throughout, team member talking: alive."""
    monkeypatch.setattr(agent_module, "_HANDOFF_SILENCE_POLL_S", 0.01)
    monkeypatch.setattr(agent_module, "HANDOFF_SILENCE_TIMEOUT_S", 0.08)
    state = _state()
    state.last_far_audio = time.monotonic() - 10.0  # front desk never makes a sound
    transport = _TeamTransport()
    talking = True

    async def team_keeps_talking():
        while talking:
            transport.last_team_audio = time.monotonic()
            await asyncio.sleep(0.01)

    task = asyncio.get_running_loop().create_task(team_keeps_talking())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_wait_for_handoff_to_end(state, transport), timeout=0.3)
    finally:
        talking = False
        task.cancel()


@pytest.mark.asyncio
async def test_both_legs_silent_still_ends_the_call(monkeypatch):
    monkeypatch.setattr(agent_module, "_HANDOFF_SILENCE_POLL_S", 0.01)
    monkeypatch.setattr(agent_module, "HANDOFF_SILENCE_TIMEOUT_S", 0.05)
    state = _state()
    state.last_far_audio = time.monotonic() - 10.0
    transport = _TeamTransport(last_team_audio=time.monotonic() - 10.0)

    assert await _wait_for_handoff_to_end(state, transport) == "handoff went silent"


@pytest.mark.asyncio
async def test_a_team_leg_that_never_spoke_does_not_extend_the_wait(monkeypatch):
    """`last_team_audio` is 0.0 until the first sounding frame. Taken literally as a
    monotonic stamp that would read as the distant past, which is what we want -- but it
    must not be mistaken for "now" and hold a dead call open forever."""
    monkeypatch.setattr(agent_module, "_HANDOFF_SILENCE_POLL_S", 0.01)
    monkeypatch.setattr(agent_module, "HANDOFF_SILENCE_TIMEOUT_S", 0.05)
    state = _state()
    state.last_far_audio = time.monotonic() - 10.0

    assert await _wait_for_handoff_to_end(state, _TeamTransport(0.0)) == "handoff went silent"


@pytest.mark.asyncio
async def test_a_transport_with_no_team_leg_is_unchanged(monkeypatch):
    """A transport with no team leg has no `last_team_audio`; the front desk alone
    still decides, and passing no transport at all behaves the same."""
    monkeypatch.setattr(agent_module, "_HANDOFF_SILENCE_POLL_S", 0.01)
    monkeypatch.setattr(agent_module, "HANDOFF_SILENCE_TIMEOUT_S", 0.05)

    class _NoTeamTransport:
        pass

    for transport in (_NoTeamTransport(), None):
        state = _state()
        state.last_far_audio = time.monotonic() - 10.0
        assert await _wait_for_handoff_to_end(state, transport) == "handoff went silent"


def test_the_team_pump_stamps_the_marker_it_claims_to():
    """Guards the wiring, which no unit test of the backstop alone can reach: the
    LiveKit pump must actually stamp `_last_team_audio`, gated on RMS the same way the
    front-desk path is."""
    import inspect

    from outbound_ivr.voice import livekit_transport

    src = inspect.getsource(livekit_transport.LiveKitTransport._pump_team_audio)
    assert "self._last_team_audio = time.monotonic()" in src
    assert "audioop.rms" in src, "must gate on sound, not on a frame merely arriving"


def test_both_legs_share_one_rms_floor():
    """One environment variable moves both, or a tuned front desk and an untuned team
    leg would disagree about what silence is."""
    from outbound_ivr.voice.agent import FAR_AUDIO_SILENCE_RMS
    from outbound_ivr.voice.livekit_transport import TEAM_AUDIO_SILENCE_RMS

    assert TEAM_AUDIO_SILENCE_RMS == FAR_AUDIO_SILENCE_RMS


def test_the_deployed_sip_timeout_fires_before_this_backstop():
    """The real hangup signal must win the race against the inferred one.

    livekit-sip removing a dead participant surfaces as `participant_disconnected`,
    which is a fact; this module's silence check is an inference from audio levels. The
    AWS runtime therefore sets OUTBOUND_IVR_SIP_MEDIA_TIMEOUT_S strictly below
    HANDOFF_SILENCE_TIMEOUT_S so the fact arrives first and the inference stays a net.

    Read out of the CDK source rather than imported: the stack's venv has no `outbound_ivr`
    and this one has no `aws_cdk`, so neither side can import the other.
    """
    import re
    from pathlib import Path

    from outbound_ivr.voice.agent import HANDOFF_SILENCE_TIMEOUT_S
    from outbound_ivr.voice.livekit_calls import SIP_MEDIA_TIMEOUT_S

    stack = (
        Path(__file__).resolve().parents[2] / "infrastructure" / "stacks" / "agent_runtime_stack.py"
    )
    match = re.search(r'"OUTBOUND_IVR_SIP_MEDIA_TIMEOUT_S":\s*"([\d.]+)"', stack.read_text())
    assert match, "the AWS runtime must pin a SIP media timeout; the 120s default is too long"
    assert 0 < float(match.group(1)) < HANDOFF_SILENCE_TIMEOUT_S

    # Left long on purpose: a legitimate hold can fall silent between turns.
    assert SIP_MEDIA_TIMEOUT_S == 120


def test_the_sip_media_timeout_is_overridable(monkeypatch):
    """It has to be readable from the environment, or the stack's setting does nothing."""
    import importlib

    from outbound_ivr.voice import livekit_calls

    monkeypatch.setenv("OUTBOUND_IVR_SIP_MEDIA_TIMEOUT_S", "15")
    try:
        assert importlib.reload(livekit_calls).SIP_MEDIA_TIMEOUT_S == 15
    finally:
        monkeypatch.delenv("OUTBOUND_IVR_SIP_MEDIA_TIMEOUT_S", raising=False)
        importlib.reload(livekit_calls)
