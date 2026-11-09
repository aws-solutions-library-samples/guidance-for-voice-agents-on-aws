"""LiveKitTransport against fakes of the livekit SDK objects: no live LiveKit server.

The fakes below are small, hand-written stand-ins for the real `livekit.rtc`/`livekit.api`
shapes (confirmed against the real `livekit`==1.1.21 / `livekit-api`==1.2.2 packages while
writing `outbound_ivr.voice.livekit_transport`), monkeypatched in through `_import_rtc`/
`_import_api` so this file never imports the real `livekit` package -- it isn't one of this
project's dependencies, in any extra these tests run under.
"""

from __future__ import annotations

import asyncio
import contextlib
import types
from typing import Any, ClassVar

import pytest

from outbound_ivr.voice import frames as outbound_ivr_frames
from outbound_ivr.voice import livekit_transport as lkt
from outbound_ivr.voice.transport import TransportError

URL = "ws://127.0.0.1:7880"
API_KEY = "devkey"
API_SECRET = "secret"  # nosec B105 -- test fixture, not a credential
ROOM_NAME = "outbound-ivr-call-1"


# --------------------------------------------------------------------------
# Fakes for livekit.rtc
# --------------------------------------------------------------------------


class FakeTrackKind:
    KIND_AUDIO = "audio"
    KIND_VIDEO = "video"


class FakeTrack:
    """A remote audio track whose simulated inbound audio is a fixed list of chunks."""

    def __init__(
        self, chunks: list[bytes] | None = None, kind: str = FakeTrackKind.KIND_AUDIO
    ) -> None:
        self.chunks = list(chunks or [])
        self.kind = kind


class FakeAudioFrame:
    def __init__(
        self, data: Any, sample_rate: int, num_channels: int, samples_per_channel: int
    ) -> None:
        self.data = data
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


class FakeAudioFrameEvent:
    def __init__(self, frame: FakeAudioFrame) -> None:
        self.frame = frame


class FakeAudioStream:
    """Replays `track.chunks`, then ends -- like a track whose stream ran out."""

    def __init__(
        self,
        track: FakeTrack,
        sample_rate: int = 8000,
        num_channels: int = 1,
        frame_size_ms: int | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.frame_size_ms = frame_size_ms
        self._chunks = list(track.chunks)

    def __aiter__(self) -> FakeAudioStream:
        return self

    async def __anext__(self) -> FakeAudioFrameEvent:
        if not self._chunks:
            raise StopAsyncIteration
        chunk = self._chunks.pop(0)
        return FakeAudioFrameEvent(
            FakeAudioFrame(chunk, self.sample_rate, self.num_channels, len(chunk) // 2)
        )


class FakeAudioSource:
    def __init__(self, sample_rate: int, num_channels: int) -> None:
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.captured: list[FakeAudioFrame] = []
        self.closed = False

    async def capture_frame(self, frame: FakeAudioFrame) -> None:
        self.captured.append(frame)

    async def aclose(self) -> None:
        self.closed = True


class FakeLocalAudioTrack:
    def __init__(self, name: str, source: FakeAudioSource) -> None:
        self.name = name
        self.source = source
        self.sid = f"TR_{name}"

    @classmethod
    def create_audio_track(cls, name: str, source: FakeAudioSource) -> FakeLocalAudioTrack:
        return cls(name, source)


class FakeLocalParticipant:
    def __init__(self) -> None:
        self.published_tracks: list[Any] = []
        self.unpublished_sids: list[str] = []
        self.dtmf_sent: list[dict] = []

    async def publish_track(self, track: Any) -> None:
        self.published_tracks.append(track)

    async def unpublish_track(self, sid: str) -> None:
        self.unpublished_sids.append(sid)

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        self.dtmf_sent.append({"code": code, "digit": digit})


class FakeTrackPublication:
    def __init__(self, track: FakeTrack | None, kind: str = FakeTrackKind.KIND_AUDIO) -> None:
        self.track = track
        self.kind = kind


class FakeRemoteParticipant:
    def __init__(
        self,
        identity: str,
        attributes: dict[str, str] | None = None,
        audio_track: FakeTrack | None = None,
    ) -> None:
        self.identity = identity
        self.attributes = attributes or {}
        self.track_publications: dict[str, FakeTrackPublication] = {}
        if audio_track is not None:
            self.track_publications["audio"] = FakeTrackPublication(audio_track)


class FakeRoom:
    def __init__(self, name: str = "", metadata: str = "") -> None:
        self.name = name
        self.metadata = metadata
        self.remote_participants: dict[str, FakeRemoteParticipant] = {}
        self.local_participant = FakeLocalParticipant()
        self._handlers: dict[str, list] = {}
        self.connect_calls: list[tuple] = []
        self.disconnected = False

    def on(self, event: str, callback=None):
        """Both forms livekit.rtc.Room.on supports: on(event, fn) and @on(event)."""

        def deco(fn):
            self._handlers.setdefault(event, []).append(fn)
            return fn

        return deco(callback) if callback is not None else deco

    def emit(self, event: str, *args: Any) -> None:
        for fn in list(self._handlers.get(event, [])):
            fn(*args)

    async def connect(self, url: str, token: str, options: Any = None) -> None:
        self.connect_calls.append((url, token, options))

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeSipDTMF:
    def __init__(self, code: int, digit: str) -> None:
        self.code = code
        self.digit = digit


def make_fake_rtc(room: FakeRoom) -> types.SimpleNamespace:
    ns = types.SimpleNamespace()
    ns.Room = lambda: room
    ns.RoomOptions = lambda **kw: kw
    ns.AudioStream = FakeAudioStream
    ns.AudioSource = FakeAudioSource
    ns.AudioFrame = FakeAudioFrame
    ns.LocalAudioTrack = FakeLocalAudioTrack
    ns.TrackKind = FakeTrackKind
    return ns


# --------------------------------------------------------------------------
# Fakes for livekit.api
# --------------------------------------------------------------------------


class FakeRoomServiceClient:
    def __init__(self) -> None:
        self.removed: list[tuple[str, str]] = []
        self.fail = False

    async def remove_participant(self, req: Any) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.removed.append((req.room, req.identity))


class FakeLiveKitAPI:
    def __init__(self, url: str, api_key: str, api_secret: str) -> None:
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self.room = FakeRoomServiceClient()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeRoomParticipantIdentity:
    def __init__(self, room: str, identity: str) -> None:
        self.room = room
        self.identity = identity


class FakeVideoGrants:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw


class FakeAccessToken:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self._identity: str | None = None
        self._grants: Any = None

    def with_identity(self, identity: str) -> FakeAccessToken:
        self._identity = identity
        return self

    def with_grants(self, grants: Any) -> FakeAccessToken:
        self._grants = grants
        return self

    def to_jwt(self) -> str:
        return f"fake-jwt-{self._identity}"


def make_fake_api(
    created: list[FakeLiveKitAPI], *, fail_remove: bool = False
) -> types.SimpleNamespace:
    ns = types.SimpleNamespace()
    ns.AccessToken = FakeAccessToken
    ns.VideoGrants = FakeVideoGrants
    ns.RoomParticipantIdentity = FakeRoomParticipantIdentity

    def _lk_api(url: str, api_key: str, api_secret: str) -> FakeLiveKitAPI:
        inst = FakeLiveKitAPI(url, api_key, api_secret)
        inst.room.fail = fail_remove
        created.append(inst)
        return inst

    ns.LiveKitAPI = _lk_api
    return ns


# --------------------------------------------------------------------------
# Test setup helper
# --------------------------------------------------------------------------


def _wire(monkeypatch, room: FakeRoom, created_apis: list[FakeLiveKitAPI] | None = None):
    created_apis = created_apis if created_apis is not None else []
    monkeypatch.setattr(lkt, "_import_rtc", lambda: make_fake_rtc(room))
    monkeypatch.setattr(lkt, "_import_api", lambda: make_fake_api(created_apis))


def _make_transport(**kw) -> lkt.LiveKitTransport:
    kw.setdefault("url", URL)
    kw.setdefault("api_key", API_KEY)
    kw.setdefault("api_secret", API_SECRET)
    kw.setdefault("room_name", ROOM_NAME)
    return lkt.LiveKitTransport(**kw)


async def _drain(transport: lkt.LiveKitTransport, count: int) -> list[outbound_ivr_frames.Frame]:
    frames = []
    async for frame in transport.frames():
        frames.append(frame)
        if len(frames) == count:
            break
    return frames


# --------------------------------------------------------------------------
# handshake: joining, call id, mode/case id resolution
# --------------------------------------------------------------------------


async def test_handshake_adopts_preexisting_participant_and_resolves_call_uuid(monkeypatch):
    room = FakeRoom(name=ROOM_NAME, metadata="")
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    call_uuid = await transport.handshake()

    assert call_uuid == ROOM_NAME
    assert transport.call_uuid == ROOM_NAME
    assert room.connect_calls and room.connect_calls[0][0] == URL


async def test_handshake_resolves_mode_and_case_from_room_metadata(monkeypatch):
    room = FakeRoom(name=ROOM_NAME, metadata='{"mode": "intake", "case_id": "AUTH-9"}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    assert transport.mode == "intake"
    assert transport.case_id == "AUTH-9"


async def test_handshake_falls_back_to_participant_attributes(monkeypatch):
    room = FakeRoom(name=ROOM_NAME, metadata="")
    participant = FakeRemoteParticipant(
        "sip_1", attributes={"mode": "full_call", "case_id": "AUTH-7"}
    )
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    assert transport.mode == "full_call"
    assert transport.case_id == "AUTH-7"


async def test_handshake_falls_back_to_demo_default_case(monkeypatch):
    from outbound_ivr import prompts

    room = FakeRoom(name=ROOM_NAME, metadata="")
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    monkeypatch.delenv("OUTBOUND_IVR_MODE", raising=False)
    monkeypatch.delenv("OUTBOUND_IVR_CASE_ID", raising=False)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    assert transport.mode == "full_call"
    assert transport.case_id == prompts.CASE["pa_reference"]


async def test_handshake_adopts_first_participant_when_no_identity_configured(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_whatever")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport()  # no sip_identity
    await transport.handshake()

    assert transport._far_participant is participant


async def test_maybe_adopt_far_end_ignores_a_monitor_identity(monkeypatch):
    """The dashboard's listen-in monitor (outbound_ivr.voice.livekit_calls.RoomMonitor) always
    connects as `MONITOR_IDENTITY_PREFIX<call_id>`; it must never become the far end,
    even in the "no sip_identity configured, adopt whoever joins first" fallback this
    app also supports."""
    room = FakeRoom(name=ROOM_NAME)
    _wire(monkeypatch, room)
    transport = _make_transport()  # no sip_identity: the permissive fallback path

    monitor = FakeRemoteParticipant(f"{lkt.MONITOR_IDENTITY_PREFIX}{ROOM_NAME}")
    transport._maybe_adopt_far_end(monitor)

    assert transport._far_participant is None


async def test_handshake_ignores_a_pre_existing_monitor_and_adopts_the_real_caller(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    monitor = FakeRemoteParticipant(f"{lkt.MONITOR_IDENTITY_PREFIX}{ROOM_NAME}")
    room.remote_participants[monitor.identity] = monitor
    _wire(monkeypatch, room)
    transport = _make_transport()  # no sip_identity configured

    task = asyncio.create_task(transport.handshake())
    await asyncio.sleep(0)  # let handshake() check the pre-existing monitor, then wait
    assert transport._far_participant is None, "the monitor must not be adopted as the far end"

    real_caller = FakeRemoteParticipant("sip_real")
    room.emit("participant_connected", real_caller)
    call_uuid = await asyncio.wait_for(task, timeout=2)

    assert call_uuid == ROOM_NAME
    assert transport._far_participant is real_caller


async def test_handshake_adopts_participant_that_joins_after_connect(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    _wire(monkeypatch, room)
    transport = _make_transport(sip_identity="sip_1")

    task = asyncio.create_task(transport.handshake())
    await asyncio.sleep(0)  # let handshake() reach the join wait
    participant = FakeRemoteParticipant("sip_1")
    room.emit("participant_connected", participant)

    call_uuid = await asyncio.wait_for(task, timeout=2)
    assert call_uuid == ROOM_NAME
    assert transport._far_participant is participant


async def test_handshake_times_out_without_a_sip_participant(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    _wire(monkeypatch, room)
    monkeypatch.setattr(lkt, "JOIN_TIMEOUT", 0.05)

    transport = _make_transport(sip_identity="sip_1")
    with pytest.raises(TransportError):
        await transport.handshake()


async def test_handshake_requires_room_name():
    transport = _make_transport(room_name="")
    with pytest.raises(TransportError):
        await transport.handshake()


# --------------------------------------------------------------------------
# Audio in: frames(), re-chunked to 320 bytes
# --------------------------------------------------------------------------


async def test_frames_yields_320_byte_chunks(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    # Two 640-byte chunks (not already 320-aligned the way frame_size_ms=20 should give
    # us) prove frames() re-chunks instead of trusting the upstream frame size.
    track = FakeTrack(chunks=[bytes([1]) * 640, bytes([2]) * 640])
    participant = FakeRemoteParticipant("sip_1", audio_track=track)
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    frames = await _drain(transport, 4)
    assert all(f.type == outbound_ivr_frames.MsgType.AUDIO for f in frames)
    assert all(len(f.payload) == outbound_ivr_frames.AUDIO_FRAME_BYTES for f in frames)
    assert b"".join(f.payload for f in frames) == bytes([1]) * 640 + bytes([2]) * 640


async def test_frames_starts_reading_a_track_subscribed_later(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")  # no audio track yet
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()
    assert transport._reader_task is None

    track = FakeTrack(chunks=[bytes(320)])
    room.emit("track_subscribed", track, FakeTrackPublication(track), participant)

    frames = await _drain(transport, 1)
    assert frames[0].type == outbound_ivr_frames.MsgType.AUDIO


async def test_start_audio_reader_resubscribes_once_the_previous_reader_is_done(monkeypatch):
    """The guard must check whether the reader task is still running, not merely whether
    one was ever created -- otherwise a resubscribe after the first reader finishes (the
    track ended, the stream errored) never starts a new one."""
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    async def _already_done() -> None:
        return None

    done_task = asyncio.create_task(_already_done())
    await done_task
    assert done_task.done()
    transport._reader_task = done_task

    track = FakeTrack(chunks=[bytes(320)])
    transport._start_audio_reader(track)

    assert transport._reader_task is not done_task
    frames = await asyncio.wait_for(_drain(transport, 1), timeout=2)
    assert frames[0].type == outbound_ivr_frames.MsgType.AUDIO


# --------------------------------------------------------------------------
# DTMF in and out
# --------------------------------------------------------------------------


async def test_sip_dtmf_received_becomes_a_dtmf_frame(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    room.emit("sip_dtmf_received", FakeSipDTMF(code=5, digit="5"))

    frames = await _drain(transport, 1)
    assert frames[0].type == outbound_ivr_frames.MsgType.DTMF
    assert frames[0].payload == b"5"


@pytest.mark.parametrize(
    ("digit", "code"),
    [("0", 0), ("5", 5), ("9", 9), ("*", 10), ("#", 11)],
)
async def test_press_key_sends_the_right_dtmf_code(monkeypatch, digit, code):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    await transport.press_key(digit)

    assert room.local_participant.dtmf_sent == [{"code": code, "digit": digit}]


async def test_press_key_rejects_a_non_dtmf_digit(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    with pytest.raises(ValueError):
        await transport.press_key("x")


# --------------------------------------------------------------------------
# Audio out
# --------------------------------------------------------------------------


async def test_send_audio_captures_a_frame_on_the_source(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    pcm = bytes(range(256)) + bytes(range(64))  # 320 bytes, arbitrary content
    await transport.send_audio(pcm)

    assert room.local_participant.published_tracks  # publish_track happened in handshake()
    captured = transport._audio_source.captured
    assert len(captured) == 1
    assert captured[0].data == pcm
    assert captured[0].sample_rate == 8000
    assert captured[0].num_channels == 1
    assert captured[0].samples_per_channel == 160


async def test_send_audio_translates_an_sdk_failure_to_transport_error(monkeypatch):
    """`AudioSource.capture_frame` can raise a plain Exception from the livekit SDK; the
    pacer in `outbound_ivr.voice.agent` only knows how to stop cleanly on the Transport
    contract's own errors (and ConnectionError/OSError), not arbitrary SDK exceptions."""
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    async def _boom(frame: FakeAudioFrame) -> None:
        raise RuntimeError("ffi call failed: PeerConnection is closed")

    transport._audio_source.capture_frame = _boom

    with pytest.raises(TransportError):
        await transport.send_audio(bytes(outbound_ivr_frames.AUDIO_FRAME_BYTES))


# --------------------------------------------------------------------------
# Hang-up and close
# --------------------------------------------------------------------------


async def test_hang_up_removes_the_sip_participant_then_disconnects(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    created_apis: list[FakeLiveKitAPI] = []
    _wire(monkeypatch, room, created_apis)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    await transport.hang_up()

    assert created_apis, "LiveKitAPI was never constructed"
    assert created_apis[-1].room.removed == [(ROOM_NAME, "sip_1")]
    assert created_apis[-1].closed is True
    assert room.disconnected is True


async def test_hang_up_tolerates_remove_participant_failure(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    # Fail the removal (simulating livekit-api being unreachable, or the participant
    # already gone) and confirm hang_up still disconnects the room instead of raising.
    created_apis: list[FakeLiveKitAPI] = []
    monkeypatch.setattr(lkt, "_import_api", lambda: make_fake_api(created_apis, fail_remove=True))

    await transport.hang_up()  # must not raise

    assert created_apis[-1].room.fail is True
    assert room.disconnected is True


async def test_close_cancels_reader_and_disconnects(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    track = FakeTrack(chunks=[bytes(320)] * 1000)  # effectively endless
    participant = FakeRemoteParticipant("sip_1", audio_track=track)
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()
    assert transport._reader_task is not None

    await transport.close()

    assert transport._reader_task.cancelled() or transport._reader_task.done()
    assert transport._audio_source.closed is True
    assert room.disconnected is True


# --------------------------------------------------------------------------
# Hangup/disconnect events end frames()
# --------------------------------------------------------------------------


async def test_participant_disconnected_ends_frames(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    room.emit("participant_disconnected", participant)

    seen = [f async for f in transport.frames()]
    assert seen == []  # ended immediately, no frames queued


async def test_room_disconnected_event_ends_frames(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    room.emit("disconnected", "SERVER_SHUTDOWN")

    seen = [f async for f in transport.frames()]
    assert seen == []


# --------------------------------------------------------------------------
# Recording: handshake() starts a CallRecorder when the room says to, feeds it from both
# the far-end frame path and send_audio, and close() always tears it down.
# --------------------------------------------------------------------------


def _wire_recording(monkeypatch, tmp_path) -> list[Any]:
    """Monkeypatch `recording_path` so recordings land under `tmp_path`, and return the
    list of `CallRecorder` instances the transport constructs (one per handshake)."""
    created: list[Any] = []
    real_recorder_cls = lkt.CallRecorder

    def _tracking_recorder(*args: Any, **kwargs: Any) -> Any:
        rec = real_recorder_cls(*args, **kwargs)
        created.append(rec)
        return rec

    monkeypatch.setattr(lkt, "recording_path", lambda call_id: tmp_path / f"{call_id}.wav")
    monkeypatch.setattr(lkt, "CallRecorder", _tracking_recorder)
    return created


async def test_handshake_starts_a_recorder_when_room_metadata_says_record(monkeypatch, tmp_path):
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": true}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    created = _wire_recording(monkeypatch, tmp_path)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    assert len(created) == 1
    assert transport._recorder is created[0]
    assert created[0]._started

    await transport.close()


async def test_handshake_defaults_to_recording_like_the_dashboard_checkbox(monkeypatch, tmp_path):
    room = FakeRoom(name=ROOM_NAME, metadata="")  # no "record" key at all
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    created = _wire_recording(monkeypatch, tmp_path)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    assert len(created) == 1
    await transport.close()


async def test_handshake_skips_the_recorder_when_room_metadata_says_no(monkeypatch, tmp_path):
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": false}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    created = _wire_recording(monkeypatch, tmp_path)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    assert created == []
    assert transport._recorder is None
    assert not (tmp_path / f"{ROOM_NAME}.wav").exists()

    await transport.close()


async def test_handshake_record_falls_back_to_participant_attribute(monkeypatch, tmp_path):
    room = FakeRoom(name=ROOM_NAME, metadata="")
    participant = FakeRemoteParticipant("sip_1", attributes={"record": "false"})
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    created = _wire_recording(monkeypatch, tmp_path)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    assert created == []
    await transport.close()


async def test_recording_path_is_the_recorders_path_once_recording(monkeypatch, tmp_path):
    """For outbound_ivr.voice.agent.upload_recording's S3 step, which reads this property to
    find the finished WAV -- gated on OUTBOUND_IVR_ARTIFACTS_BUCKET, not transport mode."""
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": true}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    _wire_recording(monkeypatch, tmp_path)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    assert transport.recording_path == tmp_path / f"{ROOM_NAME}.wav"

    await transport.close()


async def test_recording_path_is_none_when_the_call_was_never_recorded(monkeypatch, tmp_path):
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": false}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    _wire_recording(monkeypatch, tmp_path)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    assert transport.recording_path is None

    await transport.close()


async def test_far_end_frames_are_fed_to_the_recorder(monkeypatch, tmp_path):
    """The real-time 20 ms clock in CallRecorder.start() can drain `_far_buf` as soon as
    it's appended to, so this stops the clock first (cancels `_clock_task` without
    closing the recorder) to make the assertion race-free."""
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": true}')
    track = FakeTrack(chunks=[bytes([7]) * 320])
    participant = FakeRemoteParticipant("sip_1", audio_track=track)
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    created = _wire_recording(monkeypatch, tmp_path)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()
    recorder = created[0]
    recorder._clock_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await recorder._clock_task

    await _drain(transport, 1)

    assert bytes(recorder._far_buf) == bytes([7]) * 320

    await transport.close()


async def test_send_audio_is_fed_to_the_recorder(monkeypatch, tmp_path):
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": true}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    created = _wire_recording(monkeypatch, tmp_path)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()
    recorder = created[0]
    recorder._clock_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await recorder._clock_task

    await transport.send_audio(bytes([9]) * 320)

    assert bytes(recorder._near_buf) == bytes([9]) * 320

    await transport.close()


async def test_close_tears_down_the_recorder(monkeypatch, tmp_path):
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": true}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    created = _wire_recording(monkeypatch, tmp_path)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()
    recorder = created[0]

    await transport.close()

    assert recorder._closed
    assert (tmp_path / f"{ROOM_NAME}.wav").exists()


# --------------------------------------------------------------------------
# Warm handoff (docs/escalation-handoff.md): the "team-" participant.
# --------------------------------------------------------------------------


async def test_maybe_adopt_far_end_ignores_a_team_identity(monkeypatch):
    """A "team-" participant must never become the far end either, same reasoning as
    the monitor exclusion above."""
    room = FakeRoom(name=ROOM_NAME)
    _wire(monkeypatch, room)
    transport = _make_transport()  # no sip_identity: the permissive fallback path

    team = FakeRemoteParticipant(f"{lkt.TEAM_IDENTITY_PREFIX}abc")
    transport._maybe_adopt_far_end(team)

    assert transport._far_participant is None


async def test_wait_for_team_join_times_out_without_a_team_participant(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    identity = await transport.wait_for_team_join(timeout=0.05)

    assert identity is None


async def test_wait_for_team_join_resolves_once_a_team_participant_connects(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    task = asyncio.create_task(transport.wait_for_team_join(timeout=2))
    await asyncio.sleep(0)
    team = FakeRemoteParticipant("team-abc")
    room.emit("participant_connected", team)

    identity = await asyncio.wait_for(task, timeout=2)
    assert identity == "team-abc"
    assert transport._team_participant is team


async def test_team_audio_is_fed_to_the_recorder_as_the_near_leg(monkeypatch, tmp_path):
    """Part 8: once a team participant joins, its audio -- not Outbound IVR's -- feeds the
    recorder's near/`agent` buffer."""
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": true}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    created = _wire_recording(monkeypatch, tmp_path)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()
    recorder = created[0]
    recorder._clock_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await recorder._clock_task

    team_track = FakeTrack(chunks=[bytes([5]) * 320])
    team = FakeRemoteParticipant("team-abc", audio_track=team_track)
    room.emit("participant_connected", team)
    await asyncio.sleep(0)
    await transport._team_audio_task

    assert bytes(recorder._near_buf) == bytes([5]) * 320

    await transport.close()


async def test_stop_publishing_unpublishes_the_track_but_stays_in_the_room(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()
    track = transport._local_track

    await transport.stop_publishing()

    assert room.local_participant.unpublished_sids == [track.sid]
    assert room.disconnected is False
    assert transport._audio_source is None


async def test_team_participant_disconnecting_ends_the_call(monkeypatch):
    """Once a human has joined on a handoff, that leg leaving ends the call too, not
    only the office's leg (part 4, step 5: "either leg leaves")."""
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()

    team = FakeRemoteParticipant("team-abc")
    room.emit("participant_connected", team)
    await asyncio.sleep(0)

    room.emit("participant_disconnected", team)

    seen = [f async for f in transport.frames()]
    assert seen == []


# --------------------------------------------------------------------------
# Monitor: handshake() always constructs and starts a MonitorPublisher (independent
# of recording), close() always closes it, and the on_frame batching feeds it.
# --------------------------------------------------------------------------


class _FakeMonitorPublisher:
    """Stand-in for `outbound_ivr.voice.monitor.MonitorPublisher`: records what it was asked
    to do instead of touching DynamoDB."""

    instances: ClassVar[list[_FakeMonitorPublisher]] = []

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.started = False
        self.closed = False
        self.enabled = True
        self.published: list[dict[str, Any]] = []
        _FakeMonitorPublisher.instances.append(self)

    def start(self) -> None:
        self.started = True

    def publish(self, *, far_rms, agent_rms, pcm) -> None:
        self.published.append({"far_rms": far_rms, "agent_rms": agent_rms, "pcm": pcm})

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_monitor_instances():
    _FakeMonitorPublisher.instances.clear()
    yield
    _FakeMonitorPublisher.instances.clear()


def _wire_monitor(monkeypatch) -> list[_FakeMonitorPublisher]:
    monkeypatch.setattr(lkt, "MonitorPublisher", _FakeMonitorPublisher)
    return _FakeMonitorPublisher.instances


async def test_handshake_constructs_and_starts_a_monitor_even_without_recording(monkeypatch):
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": false}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    instances = _wire_monitor(monkeypatch)

    transport = _make_transport(sip_identity="sip_1")
    call_uuid = await transport.handshake()

    assert len(instances) == 1
    assert instances[0].call_id == call_uuid
    assert instances[0].started
    assert transport._monitor is instances[0]

    await transport.close()


async def test_monitor_still_gets_fed_when_the_room_setting_turns_recording_off(
    monkeypatch, tmp_path
):
    """Monitor and recording are independent: an enabled Monitor must keep receiving
    mixed-frame batches even on a call whose room `record` setting is off, and no WAV
    must be written for that call."""
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": false}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    _wire_monitor(monkeypatch)
    monkeypatch.setattr(lkt, "recording_path", lambda call_id: tmp_path / f"{call_id}.wav")

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()
    monitor = transport._monitor

    assert transport._recorder is not None
    assert transport.recording_path is None

    far = bytes([9]) * 320
    near = bytes([3]) * 320
    mixed = bytes([1]) * 320
    for _ in range(lkt.MONITOR_BATCH_FRAMES):
        transport._on_recorder_frame(far, near, mixed)

    assert len(monitor.published) == 1
    assert not (tmp_path / f"{ROOM_NAME}.wav").exists()

    await transport.close()


async def test_close_closes_the_monitor(monkeypatch):
    room = FakeRoom(name=ROOM_NAME)
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    instances = _wire_monitor(monkeypatch)

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()
    await transport.close()

    assert instances[0].closed


async def test_on_recorder_frame_batches_200ms_before_publishing(monkeypatch, tmp_path):
    room = FakeRoom(name=ROOM_NAME, metadata='{"record": true}')
    participant = FakeRemoteParticipant("sip_1")
    room.remote_participants[participant.identity] = participant
    _wire(monkeypatch, room)
    _wire_monitor(monkeypatch)
    monkeypatch.setattr(lkt, "recording_path", lambda call_id: tmp_path / f"{call_id}.wav")

    transport = _make_transport(sip_identity="sip_1")
    await transport.handshake()
    monitor = transport._monitor

    far = bytes([9]) * 320
    near = bytes([3]) * 320
    mixed = bytes([1]) * 320
    for _ in range(lkt.MONITOR_BATCH_FRAMES - 1):
        transport._on_recorder_frame(far, near, mixed)
    assert monitor.published == []  # not yet a full 200ms batch

    transport._on_recorder_frame(far, near, mixed)
    assert len(monitor.published) == 1
    chunk = monitor.published[0]
    assert chunk["pcm"] == mixed * lkt.MONITOR_BATCH_FRAMES
    assert chunk["far_rms"] >= 0
    assert chunk["agent_rms"] >= 0

    # Buffers reset after a publish -- the next frame starts a fresh batch.
    transport._on_recorder_frame(far, near, mixed)
    assert len(monitor.published) == 1

    await transport.close()
