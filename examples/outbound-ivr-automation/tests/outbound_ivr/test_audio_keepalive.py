"""`CallInputStream`'s silence keepalive: during any quiet stretch (a hold, a
ringing transfer, `transfer_to_team`'s own wait for a colleague), the model must keep
getting silence at roughly real-time cadence, not in sparse bursts.

Found live (2026-10-17): Nova Sonic raised "Timed out waiting for audio bytes"
(ValidationException) on three calls, during exactly these waits, even though the old
keepalive (one 100 ms silence frame every full second) stayed well inside the documented
"55 seconds without audio" limit -- the real constraint is closer to real-time cadence,
not just "something within 55 seconds".
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("strands")

from outbound_ivr import intake
from outbound_ivr.voice import agent


def _state() -> agent.CallState:
    class _Log:
        def write(self, type_, **fields):
            pass

    state = agent.CallState(call_uuid="call-1", mode="intake", log=_Log())
    state.record = intake.new_record()
    return state


async def test_silence_keepalive_matches_real_time_cadence_not_once_a_second():
    """With nothing queued, consecutive deltas arrive roughly every
    SILENCE_AFTER_SECONDS (now the same as the silence frame's own 100 ms duration), not
    once a second -- a 30 s wait must not leave Nova 10x behind real time."""
    state = _state()
    stream = agent.CallInputStream(state)

    deltas = []
    start = asyncio.get_running_loop().time()
    for _ in range(5):
        await stream()
        deltas.append(asyncio.get_running_loop().time() - start)

    # Five frames back to back should take roughly 5 * SILENCE_AFTER_SECONDS, not 5
    # full seconds (the old cadence).
    elapsed = deltas[-1]
    assert elapsed < 1.0, f"keepalive fell behind real time: 5 frames took {elapsed:.2f}s"


async def test_queued_audio_is_returned_immediately_without_waiting_for_the_timeout():
    state = _state()
    stream = agent.CallInputStream(state)
    await state.raw_audio.put(b"\x01\x02" * 160)

    start = asyncio.get_running_loop().time()
    delta = await stream()
    elapsed = asyncio.get_running_loop().time() - start

    assert elapsed < 0.05
    assert delta.source["bytes"] == b"\x01\x02" * 160


async def test_silence_keepalive_keeps_flowing_through_a_long_wait():
    """Simulates `transfer_to_team`'s own up-to-30s wait: nothing arrives on raw_audio
    for a while, and the keepalive must still produce frames well under
    SILENCE_AFTER_SECONDS apart throughout, not just at the start."""
    state = _state()
    stream = agent.CallInputStream(state)

    gaps = []
    loop = asyncio.get_running_loop()
    last = loop.time()
    for _ in range(10):
        await stream()
        now = loop.time()
        gaps.append(now - last)
        last = now

    assert max(gaps) <= agent.SILENCE_AFTER_SECONDS + 0.05
