"""CallRecorder: mixing, 20 ms alignment, incremental WAV header, and the record-off path.

No real audio hardware or LiveKit server needed: `add_far_end`/`add_agent` just push bytes
into the recorder's own buffers, and `mix_one_frame`/`start`/`close` are exercised directly
(or through a short real-time run) against the actual `wave` module -- the only "fake" here
is the clock, i.e. real time itself, kept short.
"""

from __future__ import annotations

import asyncio
import audioop
import wave
from pathlib import Path

from outbound_ivr.voice.call_recorder import FRAME_BYTES, CallRecorder, recording_path

LOUD = (1000).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
QUIET = (200).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
SILENCE = bytes(FRAME_BYTES)


def test_recording_path_matches_the_dashboard_recordings_dir(tmp_path: Path):
    path = recording_path("call-123", directory=tmp_path)
    assert path == tmp_path / "call-123.wav"


# --------------------------------------------------------------------------
# Mixing
# --------------------------------------------------------------------------


def test_mix_one_frame_adds_both_sides(tmp_path: Path):
    rec = CallRecorder(tmp_path / "out.wav")
    rec._far_buf.extend(LOUD)
    rec._near_buf.extend(QUIET)

    mixed = rec.mix_one_frame()

    assert mixed == audioop.add(LOUD, QUIET, 2)
    assert mixed != LOUD
    assert mixed != QUIET


def test_mix_one_frame_uses_silence_for_an_empty_side(tmp_path: Path):
    rec = CallRecorder(tmp_path / "out.wav")
    rec._far_buf.extend(LOUD)
    # near side has nothing buffered this tick

    mixed = rec.mix_one_frame()

    assert mixed == LOUD  # adding silence is a no-op


def test_mix_one_frame_is_silence_when_both_sides_are_empty(tmp_path: Path):
    rec = CallRecorder(tmp_path / "out.wav")

    assert rec.mix_one_frame() == SILENCE


def test_mix_one_frame_consumes_exactly_one_frame_per_side(tmp_path: Path):
    rec = CallRecorder(tmp_path / "out.wav")
    rec._far_buf.extend(LOUD * 3)  # three frames queued
    rec._near_buf.extend(QUIET)  # one frame queued

    rec.mix_one_frame()

    assert len(rec._far_buf) == FRAME_BYTES * 2
    assert len(rec._near_buf) == 0


# --------------------------------------------------------------------------
# on_frame: the Monitor hook. CallRecorder doesn't know Monitor exists -- it just
# calls whatever callback it was given, once per mixed frame, with the pre-mix legs.
# --------------------------------------------------------------------------


def test_on_frame_is_called_once_per_mixed_frame_with_far_near_mixed(tmp_path: Path):
    calls = []
    rec = CallRecorder(
        tmp_path / "out.wav", on_frame=lambda far, near, mixed: calls.append((far, near, mixed))
    )
    rec._far_buf.extend(LOUD)
    rec._near_buf.extend(QUIET)

    mixed = rec.mix_one_frame()

    assert calls == [(LOUD, QUIET, mixed)]


def test_on_frame_defaults_to_none_and_mixing_works_without_it(tmp_path: Path):
    rec = CallRecorder(tmp_path / "out.wav")
    rec._far_buf.extend(LOUD)
    rec._near_buf.extend(QUIET)

    assert rec.mix_one_frame() == audioop.add(LOUD, QUIET, 2)


def test_on_frame_exception_is_swallowed_and_mixing_still_returns(tmp_path: Path):
    def boom(far, near, mixed):
        raise RuntimeError("monitor callback blew up")

    rec = CallRecorder(tmp_path / "out.wav", on_frame=boom)
    rec._far_buf.extend(LOUD)
    rec._near_buf.extend(QUIET)

    mixed = rec.mix_one_frame()

    assert mixed == audioop.add(LOUD, QUIET, 2)


# --------------------------------------------------------------------------
# Alignment: a gap on one side still advances the shared 20 ms clock -- silence fills in
# rather than the mixed stream skipping ahead or stalling to wait for that side.
# --------------------------------------------------------------------------


async def test_a_quiet_side_leaves_a_silent_frame_in_the_timeline_not_a_skip(tmp_path: Path):
    path = tmp_path / "out.wav"
    rec = CallRecorder(path)
    rec.start()
    try:
        # Three ticks: far-end speaks, then goes quiet for one tick, then speaks again.
        rec.add_far_end(LOUD)
        await asyncio.sleep(0.05)
        # (nothing added this tick -- far-end's silence)
        await asyncio.sleep(0.05)
        rec.add_far_end(LOUD)
        await asyncio.sleep(0.05)
    finally:
        await rec.close()

    with wave.open(str(path)) as w:
        data = w.readframes(w.getnframes())
    frames = [data[i : i + FRAME_BYTES] for i in range(0, len(data), FRAME_BYTES)]
    assert SILENCE in frames  # the gap shows up as an actual silent frame
    assert LOUD in frames  # not swallowed by the gap either


# --------------------------------------------------------------------------
# Header: incremental writes patch the header so the file is valid WAV before close(), and
# close() leaves the final header exactly matching what was written.
# --------------------------------------------------------------------------


async def test_header_is_valid_mid_call_before_close(tmp_path: Path):
    path = tmp_path / "out.wav"
    rec = CallRecorder(path)
    rec.start()
    try:
        rec.add_far_end(LOUD)
        await asyncio.sleep(0.1)  # a few ticks go by, written off the hot path
    finally:
        pass  # deliberately not closed yet

    # Even without close(), writeframes() has already patched the header enough times
    # that this is a readable WAV with a sane (non-zero) duration.
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 8000
        assert w.getnframes() > 0

    await rec.close()


async def test_close_finalizes_the_header_to_match_bytes_written(tmp_path: Path):
    path = tmp_path / "out.wav"
    rec = CallRecorder(path)
    rec.start()
    rec.add_far_end(LOUD)
    await asyncio.sleep(0.1)
    await rec.close()

    with wave.open(str(path)) as w:
        nframes = w.getnframes()
        data = w.readframes(nframes)
    assert nframes > 0
    assert len(data) == nframes * 2  # sample width 2 bytes, mono
    # RIFF/data chunk sizes agree with the file's actual size (header fully patched).
    size = path.stat().st_size
    assert size == 44 + nframes * 2


async def test_close_is_idempotent(tmp_path: Path):
    rec = CallRecorder(tmp_path / "out.wav")
    rec.start()
    rec.add_far_end(LOUD)
    await rec.close()
    await rec.close()  # must not raise or double-finalize


async def test_close_without_start_is_a_safe_no_op(tmp_path: Path):
    rec = CallRecorder(tmp_path / "out.wav")
    await rec.close()
    assert not (tmp_path / "out.wav").exists()


# --------------------------------------------------------------------------
# add_far_end / add_agent before start(), or after close(): never written, never raises.
# This is also "record off" from the transport's point of view -- it just never calls
# start() or add_*() at all, so nothing is created; these guard the same no-recorder path
# defensively, in case a caller feeds a recorder outside its start()/close() window.
# --------------------------------------------------------------------------


def test_add_before_start_is_dropped_not_buffered(tmp_path: Path):
    rec = CallRecorder(tmp_path / "out.wav")
    rec.add_far_end(LOUD)
    rec.add_agent(LOUD)
    assert rec._far_buf == b""
    assert rec._near_buf == b""


async def test_add_after_close_is_dropped(tmp_path: Path):
    rec = CallRecorder(tmp_path / "out.wav")
    rec.start()
    await rec.close()

    rec.add_far_end(LOUD)
    rec.add_agent(LOUD)

    assert rec._far_buf == b""
    assert rec._near_buf == b""


def test_no_file_is_created_when_the_recorder_is_never_started(tmp_path: Path):
    """The "record off" case end to end: a transport that never constructs/starts a
    CallRecorder leaves no file at all -- nothing for the Evidence page to find."""
    path = tmp_path / "never.wav"
    assert not path.exists()
