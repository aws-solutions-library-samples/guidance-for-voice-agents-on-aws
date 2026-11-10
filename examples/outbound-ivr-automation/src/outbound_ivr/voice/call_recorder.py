"""CallRecorder: mixes a LiveKit call's two legs into one 8 kHz mono 16-bit WAV.

LiveKit has no built-in server-side tap for a self-hosted room, so this module does the
recording job in-process: `LiveKitTransport` (far-end frames, as received) and
`send_audio` (the agent's own frames, as sent) each hand their 320-byte 20 ms chunks to one
`CallRecorder`, which mixes them on its own 20 ms clock -- not on whatever cadence frames
happen to arrive at -- into `<call>.wav`, uploaded to S3 and played back from the
dashboard's Evidence page.

Why a fixed clock rather than mixing frames as they arrive: either leg can go quiet for a
stretch (the office between prompts, Outbound IVR between turns) without the other leg's audio
backing up behind it. Draining a fixed 320 bytes from each side's buffer every 20 ms, and
substituting silence for a side with nothing buffered, keeps the recording's timeline
real-time-accurate -- a pause in the room is a pause in the WAV, not a skip.

Non-blocking: `add_far_end`/`add_agent` just append to a `bytearray`, called straight from
`LiveKitTransport`'s audio-receive and audio-send paths with no `await` and no disk I/O.
The 20 ms clock task mixes and hands each frame to a queue; a separate writer task drains
that queue and does the actual (blocking) `wave` file write via `asyncio.to_thread`, off
the event loop. `wave.Wave_write.writeframes` patches the RIFF/data chunk sizes in the
header after every call where they're out of date (stdlib behaviour, not something this
module has to do itself), so the file closes out with the exact right header, and even an
unclean process exit between writes leaves a header close enough to the real size that
media players can still read it -- "fixed up on close" covers the one byte range
`writeframes` doesn't already patch, the final partial frame.
"""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import wave
from collections.abc import Callable
from pathlib import Path

import structlog

logger = structlog.get_logger("outbound_ivr.voice.call_recorder")

SAMPLE_RATE = 8000
NUM_CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
FRAME_MS = 20
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * SAMPLE_WIDTH * NUM_CHANNELS  # 320
FRAME_SECONDS = FRAME_MS / 1000

ROOT = Path(__file__).resolve().parents[3]
RECORDINGS_DIR = ROOT / "runs" / "recordings"

_SILENCE = bytes(FRAME_BYTES)


def recording_path(call_id: str, *, directory: Path = RECORDINGS_DIR) -> Path:
    """Where `call_id`'s recording lives locally: `runs/recordings/<call_id>.wav`,
    before `outbound_ivr.voice.agent` uploads it to `s3://OUTBOUND_IVR_ARTIFACTS_BUCKET/recordings/
    <call_id>.wav`, which is what the deployed `GET /api/recordings/{call}` (see
    `lambdas/api/handler.py`), used by the dashboard's Evidence page, actually serves."""
    return directory / f"{call_id}.wav"


class CallRecorder:
    """Mixes buffered far-end and agent audio into one WAV, one 20 ms frame at a time.

    Usage: `start()` once the call id is known, `add_far_end`/`add_agent` as audio
    arrives/is sent (any thread-safe-free async context -- both only run on the event
    loop), `close()` when the call ends. `close()` is idempotent and safe to call even if
    `start()` was never called (e.g. recording was off for this call).

    `on_frame`, if given, is called once per mixed 20 ms frame with the pre-mix
    `(far, near)` legs and the resulting `mixed` frame -- `LiveKitTransport` uses this to
    feed `outbound_ivr.voice.monitor.MonitorPublisher` without this class knowing Monitor
    exists; its own job stays "mix to WAV", nothing else.

    `write_wav=False` runs the same 20 ms mixing clock and `on_frame` callback but opens
    no WAV file and writes nothing to disk -- `LiveKitTransport` uses this to run the
    mixer (and therefore feed Monitor) on a call whose room `record` setting is off,
    without producing a recording the room owner didn't ask for. Monitor and recording
    are independent features (see the private-monitor spec, decision 3); this is what
    makes that true even though both currently need the same 20 ms mix.
    """

    def __init__(
        self,
        path: Path,
        *,
        frame_bytes: int = FRAME_BYTES,
        frame_seconds: float = FRAME_SECONDS,
        on_frame: Callable[[bytes, bytes, bytes], None] | None = None,
        write_wav: bool = True,
    ) -> None:
        self._path = path
        self._frame_bytes = frame_bytes
        self._frame_seconds = frame_seconds
        self._on_frame = on_frame
        self._write_wav = write_wav
        self._far_buf = bytearray()
        self._near_buf = bytearray()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._clock_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None
        self._wav: wave.Wave_write | None = None
        self._started = False
        self._closed = False

    @property
    def path(self) -> Path:
        """Where this recording's WAV lives (or will, once `start()` has run) -- the one
        thing a caller needs to find the finished file afterward, e.g. to upload it (see
        `outbound_ivr.voice.agent.upload_recording`)."""
        return self._path

    def start(self) -> None:
        """Start the mixing clock, and -- if `write_wav` -- open the WAV file and start
        the writer task."""
        if self._started:
            return
        self._started = True
        if self._write_wav:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._wav = wave.open(str(self._path), "wb")  # noqa: SIM115 - kept open across the call, closed in close()
            self._wav.setnchannels(NUM_CHANNELS)
            self._wav.setsampwidth(SAMPLE_WIDTH)
            self._wav.setframerate(SAMPLE_RATE)
            self._writer_task = asyncio.create_task(self._writer())
        self._clock_task = asyncio.create_task(self._clock())

    def add_far_end(self, pcm: bytes) -> None:
        """Buffer one chunk of far-end (office/caller) audio. No disk I/O; safe to call
        from the hot audio-receive path."""
        if self._started and not self._closed:
            self._far_buf.extend(pcm)

    def add_agent(self, pcm: bytes) -> None:
        """Buffer one chunk of agent (Outbound IVR) audio, as sent. No disk I/O; safe to call
        from the hot audio-send path."""
        if self._started and not self._closed:
            self._near_buf.extend(pcm)

    async def close(self) -> None:
        """Stop the clock, flush whatever's queued, and finalize the WAV header."""
        if self._closed:
            return
        self._closed = True
        if self._clock_task is not None:
            self._clock_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._clock_task
        if self._writer_task is not None:
            self._queue.put_nowait(None)
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer_task
        if self._wav is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._wav.close)

    # ----------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------

    def _take(self, buf: bytearray) -> bytes:
        if len(buf) >= self._frame_bytes:
            frame = bytes(buf[: self._frame_bytes])
            del buf[: self._frame_bytes]
            return frame
        return _SILENCE

    def mix_one_frame(self) -> bytes:
        """Drain one 20 ms frame from each side (silence for a side with nothing
        buffered) and mix them. Its own method so tests can drive one mix step directly,
        without the real-time pacing loop around it.

        If `on_frame` was given, it's invoked here with the pre-mix `(far, near)` legs
        and the resulting `mixed` frame -- the one place all three are known at once.
        Any exception from it is logged and dropped: a Monitor callback failing must
        never break this recorder's own job of writing the WAV.
        """
        far = self._take(self._far_buf)
        near = self._take(self._near_buf)
        mixed = audioop.add(far, near, SAMPLE_WIDTH)
        if self._on_frame is not None:
            try:
                self._on_frame(far, near, mixed)
            except Exception:
                logger.exception("call_recorder_on_frame_callback_failed", path=self._path)
        return mixed

    async def _clock(self) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        try:
            while True:
                frame = self.mix_one_frame()
                if self._write_wav:
                    self._queue.put_nowait(frame)
                next_at += self._frame_seconds
                await asyncio.sleep(max(0.0, next_at - loop.time()))
        except asyncio.CancelledError:
            return

    async def _writer(self) -> None:
        try:
            while True:
                frame = await self._queue.get()
                if frame is None:
                    return
                try:
                    await asyncio.to_thread(self._wav.writeframes, frame)
                except Exception:
                    logger.exception("call_recorder_frame_write_failed", path=self._path)
        except asyncio.CancelledError:
            return
