"""LiveKitTransport: the `Transport` contract (`outbound_ivr.voice.transport.Transport`)
implemented over a LiveKit room -- the deployed AgentCore Runtime's one transport,
driving `outbound_ivr.voice.agent.handle_call`.

The `livekit` package is imported lazily, inside `_import_rtc`/`_import_api` below, not at
module scope: importing `outbound_ivr.voice.livekit_transport` itself never requires the
`livekit`/`livekit-api` packages (the "livekit" extra in pyproject.toml) to be
installed. Only constructing or using a `LiveKitTransport` needs them.

Room contract: what the dispatcher (`outbound_ivr.voice.livekit_calls`) must set before, or
at, the agent joining a room for one call:

- **Room name**: one room per call. `LiveKitTransport` treats the room name as the call id
  (`self.call_uuid`). Pass it as `room_name=` or `$OUTBOUND_IVR_LIVEKIT_ROOM`.
- **Room metadata**: a JSON object, set when the room is created (or any time before the
  agent calls `handshake()`): `{"mode": "full_call" | "intake", "case_id": "<id>",
  "record": true | false}`. This is the primary, recommended way to set mode, case id,
  and whether to record: one JSON blob, visible to every participant, set once.
  `record` defaults on; `outbound_ivr.voice.livekit_calls.create_room` is what sets it from
  `POST /api/calls`'s `{"record": ...}` body.
- **SIP participant attributes** (fallback): if a key is missing from room metadata,
  `LiveKitTransport` looks for a same-named attribute (`mode`, `case_id`, `record`) on the
  far-end participant instead, before falling back to `$OUTBOUND_IVR_MODE` / `$OUTBOUND_IVR_CASE_ID` /
  on (`record` falls back to on). Useful if the dispatcher is a LiveKit SIP dispatch
  rule that stamps attributes onto the SIP participant it creates, rather than code
  that controls the room's metadata directly.
- **SIP participant identity**: the identity of the participant whose audio this
  transport bridges. Pass it as `sip_identity=` or `$OUTBOUND_IVR_LIVEKIT_SIP_IDENTITY`. If
  neither is set, the transport adopts whichever remote participant joins (or is already
  in the room) first and logs a warning; set it explicitly once more than one non-agent
  participant can be in the room (e.g. a browser front desk, later).

Everything else about the call (press_key, hang-up, audio) follows from there with no
further coordination needed from the dispatcher.
"""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import structlog

from outbound_ivr import prompts
from outbound_ivr.voice import frames
from outbound_ivr.voice.call_recorder import SAMPLE_WIDTH, CallRecorder, recording_path
from outbound_ivr.voice.monitor import MonitorPublisher
from outbound_ivr.voice.transport import Transport, TransportError

logger = structlog.get_logger("outbound_ivr.voice.livekit_transport")

SAMPLE_RATE = 8000
NUM_CHANNELS = 1
FRAME_MS = 20
FRAME_BYTES = frames.AUDIO_FRAME_BYTES  # 320: 8 kHz, 16-bit mono, 20 ms.

# How long handshake() waits for the SIP participant to join before giving up.
JOIN_TIMEOUT = float(os.environ.get("OUTBOUND_IVR_LIVEKIT_JOIN_TIMEOUT", "30"))

# RFC 4733 DTMF named telephone events, which is what LiveKit's `publish_dtmf(code=,
# digit=)` expects for `code` (confirmed against the installed `livekit` 1.1.21 SDK and
# LiveKit's SIP DTMF docs): '0'-'9' -> 0-9, '*' -> 10, '#' -> 11. The agent's own
# press_key tool only ever offers digits in DTMF_DIGITS (outbound_ivr.voice.agent), which is a
# subset of this table, so a lookup miss here would mean a new caller we don't support yet.
DTMF_CODES: dict[str, int] = {str(d): d for d in range(10)}
DTMF_CODES["*"] = 10
DTMF_CODES["#"] = 11

# Identity prefix for the dashboard's listen-in monitor (outbound_ivr.voice.livekit_calls.
# RoomMonitor): a hidden, subscribe-only participant that joins to mix the office's and
# Outbound IVR's audio for the browser's Monitor button. Never the far end, whatever
# sip_identity is or isn't set -- see `_maybe_adopt_far_end` below.
MONITOR_IDENTITY_PREFIX = "monitor-"

# Identity prefix for the human who joins mid-call on a warm handoff (docs/
# escalation-handoff.md): the Evidence team browser card over `livekit-client`, or
# (spec only, not built) an AWS SIP participant whose `participant_identity` is set the
# same way. Never the far end either, same guard as the monitor above, and watched for
# separately by `transfer_to_team` (outbound_ivr.voice.agent) via `wait_for_team_join`.
TEAM_IDENTITY_PREFIX = "team-"

# RMS below which a team-leg frame counts as no sound at all. Reads the same environment
# variable as `outbound_ivr.voice.agent`'s FAR_AUDIO_SILENCE_RMS on purpose: both legs feed one
# backstop, so one knob should move both floors together. Defined here rather than
# imported to keep this module off `agent`'s import graph.
TEAM_AUDIO_SILENCE_RMS = int(os.environ.get("OUTBOUND_IVR_FAR_AUDIO_SILENCE_RMS", "50"))

# How many consecutive 20 ms mixed frames `MonitorPublisher` batches into one ~200 ms
# chunk before computing per-leg RMS and publishing -- see `_on_recorder_frame` below
# and the private-monitor spec, part 11.
MONITOR_BATCH_FRAMES = 10


def _import_rtc() -> Any:
    """The real `livekit.rtc` module, imported on first use.

    A test monkeypatches this function (not the `livekit` package) to hand back a fake
    namespace of fake classes, so the test suite never needs `livekit` installed.
    """
    try:
        from livekit import rtc
    except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
        raise ImportError(
            "LiveKitTransport needs the 'livekit' package. Install it with "
            "`uv sync --extra strands --extra livekit`."
        ) from exc
    return rtc


def _import_api() -> Any:
    """The real `livekit.api` module, imported on first use. See `_import_rtc`."""
    try:
        from livekit import api
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "LiveKitTransport needs the 'livekit-api' package. Install it with "
            "`uv sync --extra strands --extra livekit`."
        ) from exc
    return api


class LiveKitTransport(Transport):
    """Joins one LiveKit room for one call, and bridges it to the `Transport` contract.

    Audio in: subscribes to the far-end (SIP) participant's audio track with
    `rtc.AudioStream(track, sample_rate=8000, num_channels=1, frame_size_ms=20)`, which
    asks LiveKit to do the resampling and framing; `frames()` still re-chunks whatever
    comes back into exact 320-byte pieces before yielding, so a frame_size_ms change (or
    SDK version drift) in the dependency can't silently change the contract.

    Audio out: one `rtc.AudioSource` + `rtc.LocalAudioTrack`, published once in
    `handshake()`. `send_audio()` wraps each already-paced 320-byte chunk the agent sends
    (see `CallOutputStream._pace` in `outbound_ivr.voice.agent`) in one `rtc.AudioFrame`
    and hands it to the source; no extra pacing is added here, by design, since the
    agent already paces to real time.

    DTMF out: `press_key` calls `publish_dtmf` and returns once the call completes;
    there is no ack to wait for.

    DTMF in: a `sip_dtmf_received` room event becomes a `frames.MsgType.DTMF` frame from
    `frames()`: `read_audio` in `outbound_ivr.voice.agent` only logs these, so no further
    plumbing was needed.

    Hang-up: removes the SIP participant with `livekit-api`'s `RemoveParticipant` (so the
    actual phone call ends, not just this agent's view of it), then disconnects from the
    room.

    Recording: when the room's `record` setting (see the module docstring, "Room
    contract") is on, `handshake()` starts a `CallRecorder` at `runs/recordings/
    <call_uuid>.wav`. Every far-end frame `_pump_audio` receives and every chunk
    `send_audio` sends is also handed to the recorder (buffered, no extra disk I/O on
    those hot paths); the recorder mixes the two on its own 20 ms clock, independent of
    either side's actual frame cadence, so silence on one leg doesn't skip time in the
    recording. `close()` always finalizes and closes the recorder, whatever else happened
    this call.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        room_name: str | None = None,
        sip_identity: str | None = None,
        identity: str | None = None,
        token: str | None = None,
    ) -> None:
        self._url = url or os.environ.get("LIVEKIT_URL", "")
        self._api_key = api_key or os.environ.get("LIVEKIT_API_KEY", "")
        self._api_secret = api_secret or os.environ.get("LIVEKIT_API_SECRET", "")
        self._room_name = room_name or os.environ.get("OUTBOUND_IVR_LIVEKIT_ROOM", "")
        self._sip_identity = (
            sip_identity or os.environ.get("OUTBOUND_IVR_LIVEKIT_SIP_IDENTITY") or None
        )
        self._identity = identity or os.environ.get(
            "OUTBOUND_IVR_LIVEKIT_AGENT_IDENTITY", "outbound_ivr"
        )
        self._token = token

        self.call_uuid = ""
        self.mode = ""
        self.case_id: str | None = None

        self._rtc: Any = None
        self._room: Any = None
        self._far_participant: Any = None
        self._audio_source: Any = None
        self._local_track: Any = None
        self._reader_task: asyncio.Task | None = None
        self._audio_queue: asyncio.Queue = asyncio.Queue()
        self._joined = asyncio.Event()
        self._ended = asyncio.Event()
        self._recorder: CallRecorder | None = None
        self._record = False
        self._monitor: MonitorPublisher | None = None
        # Buffers for `_on_recorder_frame`'s 200 ms batching -- see MONITOR_BATCH_FRAMES.
        self._monitor_batch_far = bytearray()
        self._monitor_batch_near = bytearray()
        self._monitor_batch_mixed = bytearray()
        self._monitor_batch_count = 0

        # The "team-" participant a warm handoff brings onto the call (docs/
        # escalation-handoff.md parts 2-4, 8): tracked separately from `_far_participant`.
        self._team_participant: Any = None
        self._team_joined = asyncio.Event()
        self._team_audio_task: asyncio.Task | None = None
        # When the team leg last carried actual sound, for the handoff silence backstop
        # in `outbound_ivr.voice.agent`. 0.0 until the first such frame, which reads as "never"
        # to a caller comparing against `time.monotonic()` deltas.
        self._last_team_audio: float = 0.0

    @property
    def last_team_audio(self) -> float:
        """`time.monotonic()` of the last team-leg frame carrying sound, 0.0 if never.

        Read by the handoff silence backstop so a warm-transfer conversation counts as
        alive while either leg is talking. See `_pump_team_audio`.
        """
        return self._last_team_audio

    @property
    def peer(self) -> str:
        return f"livekit-room:{self._room_name or self.call_uuid}"

    @property
    def recording_path(self) -> Path | None:
        """The finished WAV's path once `close()` has finalized it, or `None` if this
        call was never recorded (room `record` setting off, or `handshake()` never
        ran). For `outbound_ivr.voice.agent.upload_recording`'s S3 step, which gates on
        `OUTBOUND_IVR_ARTIFACTS_BUCKET` being set, not on this attribute.

        Gated on `self._record`, not merely on `self._recorder` being set: a
        `CallRecorder` may exist purely to drive Monitor's mixing clock
        (`write_wav=False`) on a call whose room `record` setting is off, in which
        case no WAV was ever written and there is nothing to upload."""
        return self._recorder.path if self._record and self._recorder is not None else None

    # ----------------------------------------------------------------------
    # Transport interface
    # ----------------------------------------------------------------------

    async def handshake(self) -> str:
        if not self._room_name:
            raise TransportError(
                "LiveKitTransport needs a room name (OUTBOUND_IVR_LIVEKIT_ROOM or room_name=)"
            )
        if not self._url:
            raise TransportError("LiveKitTransport needs a server URL (LIVEKIT_URL or url=)")

        self._rtc = _import_rtc()
        self._room = self._rtc.Room()
        self._register_handlers()

        token = self._token or self._build_token()
        try:
            await self._room.connect(
                self._url, token, options=self._rtc.RoomOptions(auto_subscribe=True)
            )
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError(
                f"failed to connect to LiveKit room {self._room_name!r}: {exc}"
            ) from exc

        self.call_uuid = getattr(self._room, "name", "") or self._room_name

        # A SIP participant that joined before we did (the dispatcher dials out, the PSTN
        # callee answers, then this worker joins): adopt it now. Anyone who joins after
        # this point is caught by the "participant_connected" handler instead.
        for participant in list(self._room.remote_participants.values()):
            self._maybe_adopt_far_end(participant)
            self._maybe_adopt_team(participant)

        try:
            await asyncio.wait_for(self._joined.wait(), timeout=JOIN_TIMEOUT)
        except TimeoutError as exc:
            raise TransportError(
                f"no SIP participant joined room {self._room_name!r} within {JOIN_TIMEOUT:.0f}s"
            ) from exc

        self.mode, self.case_id, record = self._resolve_room_settings()
        self._record = record
        # Monitor and recording are independent features (see the private-monitor
        # spec, decision 3): start() is unconditional and a no-op when its bucket env
        # var is unset, regardless of whether this call is being recorded. That
        # independence has to hold even when `record` is off: `_on_recorder_frame`
        # (the only thing that ever calls `self._monitor.publish()`) is driven by
        # `CallRecorder`'s 20 ms mixing clock, so a `CallRecorder` is still created
        # here -- with `write_wav=record` -- whenever either recording or Monitor is
        # wanted, so Monitor keeps getting fed even on an unrecorded call.
        self._monitor = MonitorPublisher(self.call_uuid)
        self._monitor.start()
        if record or self._monitor.enabled:
            self._recorder = CallRecorder(
                recording_path(self.call_uuid),
                on_frame=self._on_recorder_frame,
                write_wav=record,
            )
            self._recorder.start()
        await self._publish_agent_track()
        return self.call_uuid

    async def frames(self) -> AsyncIterator[frames.Frame]:
        while True:
            item = await self._audio_queue.get()
            if item is None:
                return
            yield item

    async def send_audio(self, pcm: bytes) -> None:
        if self._audio_source is None:
            return
        samples_per_channel = len(pcm) // 2 // NUM_CHANNELS
        frame = self._rtc.AudioFrame(
            data=pcm,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            samples_per_channel=samples_per_channel,
        )
        if self._recorder is not None:
            self._recorder.add_agent(pcm)
        try:
            await self._audio_source.capture_frame(frame)
        except Exception as exc:
            # The livekit SDK can raise a plain Exception here (e.g. the room/track is
            # gone); translate it to the Transport contract's own error so callers like
            # agent.py's output pacer -- which only knows about transport-level failures,
            # not this SDK's exception types -- can tell the connection is gone and stop.
            raise TransportError(
                f"call=<{self.call_uuid}> | failed to send audio to LiveKit: {exc}"
            ) from exc

    async def press_key(self, digit: str) -> None:
        code = DTMF_CODES.get(digit)
        if code is None:
            raise ValueError(f"'{digit}' is not a DTMF digit LiveKit can send (0-9, *, #)")
        await self._room.local_participant.publish_dtmf(code=code, digit=digit)

    async def hang_up(self) -> None:
        """End the call: remove the SIP participant (the actual phone leg), then leave.

        Removing the SIP participant is what ends the call for the far end; simply
        disconnecting this agent from the room would leave the SIP participant (and
        whatever it's bridged to on the PSTN/SIP side) sitting on a now-empty room.
        """
        identity = self._sip_identity or (
            self._far_participant.identity if self._far_participant else None
        )
        if identity:
            api = _import_api()
            lk_api = api.LiveKitAPI(self._url, self._api_key, self._api_secret)
            try:
                await lk_api.room.remove_participant(
                    api.RoomParticipantIdentity(
                        room=self.call_uuid or self._room_name, identity=identity
                    )
                )
            except Exception:
                logger.exception(
                    "sip_participant_remove_failed",
                    call_id=self.call_uuid,
                    participant_identity=identity,
                )
            finally:
                with contextlib.suppress(Exception):
                    await lk_api.aclose()
        self._end_call()
        if self._room is not None:
            with contextlib.suppress(Exception):
                await self._room.disconnect()

    async def close(self) -> None:
        self._end_call()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._team_audio_task is not None:
            self._team_audio_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._team_audio_task
        if self._audio_source is not None:
            with contextlib.suppress(Exception):
                await self._audio_source.aclose()
        if self._room is not None:
            with contextlib.suppress(Exception):
                await self._room.disconnect()
        if self._recorder is not None:
            await self._recorder.close()
        if self._monitor is not None:
            await self._monitor.close()

    def _on_recorder_frame(self, far: bytes, near: bytes, mixed: bytes) -> None:
        """`CallRecorder`'s `on_frame` hook: batch `MONITOR_BATCH_FRAMES` consecutive
        20 ms frames (~200 ms) into one chunk, then publish it to `MonitorPublisher`
        with the per-leg RMS computed from the pre-mix `far`/`near` buffers (never the
        mixed audio -- the dashboard's speaking indicators need to tell which side is
        talking) and the mixed audio itself as the chunk's `pcm`.
        """
        self._monitor_batch_far.extend(far)
        self._monitor_batch_near.extend(near)
        self._monitor_batch_mixed.extend(mixed)
        self._monitor_batch_count += 1
        if self._monitor_batch_count < MONITOR_BATCH_FRAMES:
            return
        far_rms = audioop.rms(bytes(self._monitor_batch_far), SAMPLE_WIDTH)
        agent_rms = audioop.rms(bytes(self._monitor_batch_near), SAMPLE_WIDTH)
        pcm = bytes(self._monitor_batch_mixed)
        self._monitor_batch_far.clear()
        self._monitor_batch_near.clear()
        self._monitor_batch_mixed.clear()
        self._monitor_batch_count = 0
        if self._monitor is not None:
            self._monitor.publish(far_rms=far_rms, agent_rms=agent_rms, pcm=pcm)

    # ----------------------------------------------------------------------
    # Room wiring
    # ----------------------------------------------------------------------

    def _build_token(self) -> str:
        if not (self._api_key and self._api_secret):
            raise TransportError(
                "LiveKitTransport needs LIVEKIT_API_KEY/LIVEKIT_API_SECRET (or an explicit token=)"
            )
        api = _import_api()
        grants = api.VideoGrants(room_join=True, room=self._room_name)
        return (
            api.AccessToken(self._api_key, self._api_secret)
            .with_identity(self._identity)
            .with_grants(grants)
            .to_jwt()
        )

    def _register_handlers(self) -> None:
        room = self._room

        def _on_participant_connected(participant: Any) -> None:
            self._maybe_adopt_far_end(participant)
            self._maybe_adopt_team(participant)

        def _on_track_subscribed(track: Any, publication: Any, participant: Any) -> None:
            far_identity = self._far_participant.identity if self._far_participant else None
            team_identity = self._team_participant.identity if self._team_participant else None
            is_audio = getattr(track, "kind", None) == self._rtc.TrackKind.KIND_AUDIO
            if not is_audio:
                return
            if far_identity is not None and participant.identity == far_identity:
                self._start_audio_reader(track)
            elif team_identity is not None and participant.identity == team_identity:
                self._start_team_audio_reader(track)

        def _on_sip_dtmf(sip_dtmf: Any) -> None:
            digit = str(getattr(sip_dtmf, "digit", ""))
            logger.info(
                "dtmf_received_from_caller",
                call_id=self.call_uuid or self._room_name,
                digit=digit,
            )
            payload = digit.encode("ascii", "replace")
            self._audio_queue.put_nowait(frames.Frame(type=frames.MsgType.DTMF, payload=payload))

        def _on_participant_disconnected(participant: Any) -> None:
            # Either leg leaving ends the call (docs/escalation-handoff.md part 4, step
            # 5): the far end (the office), or, once a warm handoff has brought one on,
            # the team participant -- whichever of the two is tracked and leaves first.
            far_identity = self._far_participant.identity if self._far_participant else None
            team_identity = self._team_participant.identity if self._team_participant else None
            if (
                participant.identity in (far_identity, team_identity)
                and participant.identity is not None
            ):
                self._end_call()

        def _on_room_disconnected(*_args: Any) -> None:
            self._end_call()

        # Registered explicitly rather than with @room.on(...) decorators: identical
        # behaviour, and static analysers can see the handlers are used.
        room.on("participant_connected", _on_participant_connected)
        room.on("track_subscribed", _on_track_subscribed)
        room.on("sip_dtmf_received", _on_sip_dtmf)
        room.on("participant_disconnected", _on_participant_disconnected)
        room.on("disconnected", _on_room_disconnected)

    def _maybe_adopt_far_end(self, participant: Any) -> None:
        if self._far_participant is not None:
            return
        # The dashboard's listen-in monitor (hidden, and it never publishes a track
        # anyway): never the far end, with or without an explicit sip_identity.
        if participant.identity.startswith(MONITOR_IDENTITY_PREFIX):
            return
        # The warm-handoff participant (browser Evidence team card, or an AWS SIP leg):
        # never the far end either, same reasoning as the monitor above -- it is
        # adopted separately by `_maybe_adopt_team`.
        if participant.identity.startswith(TEAM_IDENTITY_PREFIX):
            return
        if self._sip_identity and participant.identity != self._sip_identity:
            return
        if not self._sip_identity:
            logger.warning(
                "no_sip_identity_configured_adopting_participant_as_far_end",
                call_id=self.call_uuid or self._room_name,
                participant_identity=participant.identity,
            )
        self._far_participant = participant
        self._joined.set()
        for pub in participant.track_publications.values():
            if (
                pub.track is not None
                and getattr(pub, "kind", None) == self._rtc.TrackKind.KIND_AUDIO
            ):
                self._start_audio_reader(pub.track)
                break

    def _start_audio_reader(self, track: Any) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            return  # already streaming; track_subscribed can fire again on resubscribe
        stream = self._rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=NUM_CHANNELS, frame_size_ms=FRAME_MS
        )
        self._reader_task = asyncio.create_task(self._pump_audio(stream))

    async def _pump_audio(self, stream: Any) -> None:
        """Re-chunk whatever `AudioStream` yields into exact 320-byte (20 ms) frames.

        `AudioStream(..., frame_size_ms=20)` already asks LiveKit for 20 ms frames at
        8 kHz mono, which is 320 bytes; buffering and re-slicing here means a version
        where that comes back as something else still can't break the 320-byte contract
        `outbound_ivr.voice.agent` and the other two transports share.
        """
        buf = bytearray()
        try:
            async for event in stream:
                buf.extend(bytes(event.frame.data))
                while len(buf) >= FRAME_BYTES:
                    payload = bytes(buf[:FRAME_BYTES])
                    del buf[:FRAME_BYTES]
                    if self._recorder is not None:
                        self._recorder.add_far_end(payload)
                    await self._audio_queue.put(
                        frames.Frame(type=frames.MsgType.AUDIO, payload=payload)
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A clean end of stream (the track unsubscribed, the participant left) is
            # StopAsyncIteration, which the `async for` above already consumes; anything
            # that lands here is the FFI binding itself failing, worth a full traceback.
            logger.exception(
                "livekit_audio_stream_ended_unexpectedly",
                call_id=self.call_uuid or self._room_name,
            )
        finally:
            self._end_call()

    async def _publish_agent_track(self) -> None:
        self._audio_source = self._rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
        self._local_track = self._rtc.LocalAudioTrack.create_audio_track(
            "outbound_ivr", self._audio_source
        )
        await self._room.local_participant.publish_track(self._local_track)

    # ----------------------------------------------------------------------
    # Warm handoff (docs/escalation-handoff.md): the "team-" participant.
    # ----------------------------------------------------------------------

    def _maybe_adopt_team(self, participant: Any) -> None:
        """Notice a `"team-"` participant joining -- the human a warm handoff brought
        onto the call -- and start reading its audio for the recording's near leg
        (part 8: "only one of Outbound IVR or the team member is ever the near-side speaker
        at a given point")."""
        if self._team_participant is not None:
            return
        if not participant.identity.startswith(TEAM_IDENTITY_PREFIX):
            return
        self._team_participant = participant
        self._team_joined.set()
        for pub in participant.track_publications.values():
            if (
                pub.track is not None
                and getattr(pub, "kind", None) == self._rtc.TrackKind.KIND_AUDIO
            ):
                self._start_team_audio_reader(pub.track)
                break

    async def wait_for_team_join(self, timeout: float) -> str | None:
        """Wait up to `timeout` seconds for a `"team-"` participant to join this room.

        Returns that participant's identity once it has joined, or `None` if nobody did
        within the timeout -- `TeamDialer.request_handoff` (outbound_ivr.voice.backend) turns
        that into a `HandoffResult`, which `transfer_to_team` (outbound_ivr.voice.agent) uses
        to decide between the warm introduction and the "no one answered" fallback.
        """
        try:
            await asyncio.wait_for(self._team_joined.wait(), timeout=timeout)
        except TimeoutError:
            return None
        return self._team_participant.identity if self._team_participant else None

    async def stop_publishing(self) -> None:
        """Go silent, but stay in the room: part 4, step 4 of the handoff contract.

        Unlike `hang_up()`, this does not remove the far-end SIP participant and does
        not disconnect from the room -- the office and the team member keep talking to
        each other after Outbound IVR's own `BidiAgent` session has ended.
        """
        if self._room is not None and self._local_track is not None:
            with contextlib.suppress(Exception):
                await self._room.local_participant.unpublish_track(self._local_track.sid)
        self._audio_source = None

    def _start_team_audio_reader(self, track: Any) -> None:
        if self._team_audio_task is not None and not self._team_audio_task.done():
            return  # already streaming; track_subscribed can fire again on resubscribe
        stream = self._rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=NUM_CHANNELS, frame_size_ms=FRAME_MS
        )
        self._team_audio_task = asyncio.create_task(self._pump_team_audio(stream))

    async def _pump_team_audio(self, stream: Any) -> None:
        """Feed the team participant's audio into the recorder's near/`agent` buffer --
        the same buffer `send_audio` fed before the handoff -- so the recording's near
        leg switches from Outbound IVR's own voice to the team member's without a third buffer
        or a change to `CallRecorder`'s mixing clock (part 8). Not fed to
        `_audio_queue`: Outbound IVR's own `BidiAgent` session has already ended by the time a
        team participant can join, so there is nothing left to listen to this audio.

        It is still timed, though. The handoff silence backstop (`outbound_ivr.voice.agent`'s
        `_wait_for_handoff_to_end`) ends a handed-off call once the line goes quiet, and
        a three-way call where only the team member is talking is very much alive. Before
        this leg was timed the backstop watched the front desk alone, so a front-desk
        person listening quietly for 20 seconds looked identical to one who had hung up.
        """
        buf = bytearray()
        try:
            async for event in stream:
                buf.extend(bytes(event.frame.data))
                while len(buf) >= FRAME_BYTES:
                    payload = bytes(buf[:FRAME_BYTES])
                    del buf[:FRAME_BYTES]
                    if audioop.rms(payload, SAMPLE_WIDTH) > TEAM_AUDIO_SILENCE_RMS:
                        self._last_team_audio = time.monotonic()
                    if self._recorder is not None:
                        self._recorder.add_agent(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "team_participant_audio_stream_ended_unexpectedly",
                call_id=self.call_uuid or self._room_name,
            )

    def _resolve_room_settings(self) -> tuple[str, str | None, bool]:
        """Room metadata first, then the far-end participant's attributes, then the same
        env-var/demo-case fallback the other two transports use. See the module docstring,
        "Room contract", for what the dispatcher sets. Returns (mode, case_id, record)."""
        meta: dict[str, Any] = {}
        raw = getattr(self._room, "metadata", "") or ""
        if raw:
            try:
                meta = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "room_metadata_not_valid_json", call_id=self.call_uuid, raw=raw[:200]
                )
        attrs: dict[str, str] = {}
        if self._far_participant is not None:
            attrs = getattr(self._far_participant, "attributes", None) or {}
        mode = (
            meta.get("mode")
            or attrs.get("mode")
            or os.environ.get("OUTBOUND_IVR_MODE", "full_call")
        )
        case_id = (
            meta.get("case_id")
            or attrs.get("case_id")
            or os.environ.get("OUTBOUND_IVR_CASE_ID")
            or prompts.CASE["auth_ref"]
        )
        record = self._resolve_bool(meta.get("record"), attrs.get("record"), default=True)
        return mode, case_id, record

    @staticmethod
    def _resolve_bool(meta_value: Any, attr_value: Any, *, default: bool) -> bool:
        """`record`'s own three-way lookup (room metadata, then participant attribute,
        then a default), tolerant of metadata's native JSON bool and an attribute's
        string form (SIP dispatch rules can only stamp string attributes)."""
        if meta_value is not None:
            return bool(meta_value)
        if attr_value is not None:
            if isinstance(attr_value, str):
                return attr_value.strip().lower() not in ("false", "0", "no", "off", "")
            return bool(attr_value)
        return default

    def _end_call(self) -> None:
        if self._ended.is_set():
            return
        self._ended.set()
        self._audio_queue.put_nowait(None)
