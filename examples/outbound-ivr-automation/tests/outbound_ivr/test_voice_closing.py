"""The agent ends a call itself after complete/escalate, once the goodbye has played."""

import asyncio
import base64

import pytest

pytest.importorskip("strands.bidi")

from strands.bidi.models.model import BidiModel
from strands.bidi.types.events import (
    BidiAudioDeltaEvent,
    BidiResponseStartEvent,
    BidiResponseStopEvent,
    BidiTranscriptBlockEvent,
)

from outbound_ivr.voice import agent, frames


def _audio_delta(pcm: bytes) -> BidiAudioDeltaEvent:
    return BidiAudioDeltaEvent(
        audio=base64.b64encode(pcm).decode(),
        format="pcm",
        sample_rate=16000,
        channels=1,
        content_id="c",
    )


def _transcript(text: str, role: str = "assistant") -> BidiTranscriptBlockEvent:
    return BidiTranscriptBlockEvent(transcript=text, role=role, content_id="c")


class _Conn:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)


def _state() -> agent.CallState:
    class _Log:
        def write(self, *a, **k) -> None:
            pass

    return agent.CallState(call_uuid="c", mode="intake", log=_Log())


def test_partial_frame_is_not_playing():
    out = agent.CallOutputStream(_Conn(), _state())
    out._buf.extend(bytes(frames.AUDIO_FRAME_BYTES - 2))
    assert not out.playing
    out._buf.extend(bytes(2))
    assert out.playing


@pytest.mark.parametrize("exc_type", [ConnectionError, OSError, agent.TransportError])
async def test_pace_exits_cleanly_when_send_audio_fails(exc_type):
    """The pacer must return quietly on any transport-level send failure -- including a
    LiveKitTransport translating an SDK error into TransportError -- instead of letting
    the exception escape the task (which `stop()` would otherwise re-raise later)."""

    class _FailingConn:
        async def send_audio(self, pcm: bytes) -> None:
            raise exc_type("boom")

    out = agent.CallOutputStream(_FailingConn(), _state())
    out._buf.extend(bytes(frames.AUDIO_FRAME_BYTES))

    await asyncio.wait_for(out._pace(), timeout=1)  # must return, not raise


async def test_closing_ends_phase_after_goodbye_plays(monkeypatch):
    monkeypatch.setattr(agent, "CLOSING_QUIET_SECONDS", 0.1)
    state = _state()
    out = agent.CallOutputStream(_Conn(), state)
    out._buf.extend(bytes(frames.AUDIO_FRAME_BYTES * 3 + 100))  # 60 ms + an unplayable tail
    out._pacer = asyncio.create_task(out._pace())
    watcher = asyncio.create_task(agent.watch_closing(state, out))
    state.begin_closing()
    await asyncio.wait_for(state.done.wait(), timeout=2)
    assert len(out._conn.sent) == 3
    out._pacer.cancel()
    watcher.cancel()


async def test_closing_waits_for_a_late_reply_instead_of_cutting_it_off(monkeypatch):
    """`transfer_to_team`'s warm introduction is only composed after the tool call
    returns, so there can be a real silent gap -- not dead air to cut short -- between
    `begin_closing` and the model's first frame. Quiet alone, before anything has
    played, must not end the phase; only `CLOSING_MAX_SECONDS` may, as an absolute cap
    for a model that never speaks at all."""
    monkeypatch.setattr(agent, "CLOSING_QUIET_SECONDS", 0.1)
    monkeypatch.setattr(agent, "CLOSING_MAX_SECONDS", 1.0)
    state = _state()
    out = agent.CallOutputStream(_Conn(), state)
    out._pacer = asyncio.create_task(out._pace())
    watcher = asyncio.create_task(agent.watch_closing(state, out))
    state.begin_closing()

    await asyncio.sleep(0.3)  # longer than CLOSING_QUIET_SECONDS, nothing has played yet
    assert not state.done.is_set(), "must not end the phase before the model has said anything"

    out._buf.extend(bytes(frames.AUDIO_FRAME_BYTES))  # the late reply finally arrives
    await asyncio.wait_for(state.done.wait(), timeout=2)
    assert len(out._conn.sent) == 1
    out._pacer.cancel()
    watcher.cancel()


class _SilentModel(BidiModel):
    """A Nova Sonic stand-in that connects and then never says anything."""

    def __init__(self) -> None:
        self.sent: list = []
        self.stopped = False

    @property
    def model_id(self) -> str:
        return "silent"

    def get_audio_config(self) -> dict:
        return {"input": {"sample_rate": 8000}, "output": {"sample_rate": 8000}}

    def get_connection_config(self) -> dict:
        return {}

    def get_config(self) -> dict:
        return {}

    def update_config(self, **kwargs) -> None:
        pass

    async def start(self, system_prompt=None, tools=None, messages=None, **kwargs) -> None:
        pass

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, content) -> None:
        self.sent.append(content)

    async def receive(self):
        await asyncio.Event().wait()  # the model never sends another event
        yield None


async def test_phase_ends_on_finish_even_if_model_is_silent():
    """finish_phase ends BidiAgent.run() promptly and closes the model session."""
    state = _state()
    model = _SilentModel()
    state.raw_audio.put_nowait(bytes(frames.AUDIO_FRAME_BYTES))
    asyncio.get_running_loop().call_later(0.2, state.finish_phase, None)
    await asyncio.wait_for(
        agent.run_phase(_Conn(), state, model, "prompt", [], opening=None, phase="intake"),
        timeout=3,
    )
    assert model.stopped
    assert model.sent, "call audio should have reached the model"


async def test_opening_is_sent_first():
    state = _state()
    model = _SilentModel()
    asyncio.get_running_loop().call_later(0.1, state.finish_phase, None)
    await asyncio.wait_for(
        agent.run_phase(_Conn(), state, model, "prompt", [], opening="Hello", phase="intake"),
        timeout=3,
    )
    assert "Hello" in str(model.sent[0])


def test_level_scale():
    assert agent._level(bytes(640)) == 0.0  # silence
    loud = (b"\xff\x7f" + b"\x01\x80") * 160  # full-scale square wave
    assert agent._level(loud) == 1.0
    import numpy as np

    quiet = (np.sin(np.arange(320) / 3) * 300).astype("<i2").tobytes()  # about -43 dBFS
    assert 0.05 < agent._level(quiet) < 0.3


def test_tool_audit_records_each_call():
    from types import SimpleNamespace

    state = _state()
    audit = agent.ToolAudit(state, "intake")
    audit._after(
        SimpleNamespace(
            tool_use={"name": "record_item", "input": {"key": "progress_note", "value": "Sept 22"}},
            result={"status": "success", "content": [{"text": "Saved."}]},
            duration=0.004,
            exception=None,
        )
    )
    entry = state.record["tool_calls"][0]
    assert entry["tool"] == "record_item" and entry["phase"] == "intake"
    assert entry["status"] == "success" and entry["result"] == "Saved." and entry["ms"] == 4


def test_greeting_during_transfer_is_detected():
    heard = [
        "for imaging press three",
        "to go back thank you please hold while i transfer you to our prior authorization desk any company medical this is sam",
    ]
    assert agent.greeted_during_transfer(heard)
    assert not agent.greeted_during_transfer(
        ["please hold while i transfer you to our prior authorization desk"]
    )
    assert not agent.greeted_during_transfer(
        ["thank you for calling, this is a demonstration phone system"]
    )


# --------------------------------------------------------------------------
# Stage directions ("[Silence]", "<silent>", "(silence)", "Thinking Process: ...")
# never get spoken or logged as said -- part 4 of the pushback fix.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[Silence]",
        "(silence)",
        "(Wait quietly)",
        "<silent>",
        "<silent></silent>",
        "<silent>\n</silent>",
        "<silent> Thinking Process: I should wait here.</silent>",
        "Thinking Process: I should wait quietly for the office to come back.",
    ],
)
def test_is_stage_direction_recognizes_every_known_form(text):
    assert agent._is_stage_direction(text)


@pytest.mark.parametrize(
    "text",
    [
        "Sure, I'll hold.",
        "Thanks for picking this up.",
        "I don't know about any thinking process they use.",
    ],
)
def test_is_stage_direction_does_not_flag_real_speech(text):
    assert not agent._is_stage_direction(text)


def test_say_does_not_log_a_stage_direction():
    state = _state()
    state.say("[Silence]")
    assert state.record["transcript"] == []


def test_say_logs_real_speech_after_stripping_a_tag():
    state = _state()
    state.say("<silent>Thinking Process: hmm</silent>Sure, I'll hold.")
    assert state.record["transcript"] == [
        {"who": "agent", "text": "Sure, I'll hold.", "t": state.record["transcript"][0]["t"]}
    ]


async def test_output_stream_suppresses_audio_for_a_stage_direction_response():
    """Text arrives, text is a stage direction: the audio for that response is dropped,
    not played -- even though BidiAudioDeltaEvent arrived first, as it does in practice
    before the transcript block."""
    state = _state()
    out = agent.CallOutputStream(_Conn(), state)
    out._in_rate = agent.INPUT_SAMPLE_RATE

    await out(BidiResponseStartEvent(response_id="r1"))
    await out(_audio_delta(bytes(agent.frames.AUDIO_FRAME_BYTES * 4)))  # audio before text
    await out(_transcript("[Silence]"))
    await out(BidiResponseStopEvent(response_id="r1"))

    assert bytes(out._buf) == b""
    assert bytes(out._pending_audio) == b""


async def test_output_stream_plays_audio_once_text_shows_real_speech():
    state = _state()
    out = agent.CallOutputStream(_Conn(), state)
    out._in_rate = agent.INPUT_SAMPLE_RATE

    await out(BidiResponseStartEvent(response_id="r1"))
    pcm = bytes(agent.frames.AUDIO_FRAME_BYTES * 4)
    await out(_audio_delta(pcm))  # audio before text, held back
    await out(_transcript("Sure, I'll hold."))  # real speech: release what was held
    more_pcm = bytes(agent.frames.AUDIO_FRAME_BYTES * 2)
    await out(_audio_delta(more_pcm))  # arrives after the decision: played directly
    await out(BidiResponseStopEvent(response_id="r1"))

    assert len(out._buf) == len(pcm) + len(more_pcm)
    assert bytes(out._pending_audio) == b""


async def test_output_stream_plays_audio_immediately_when_text_comes_first():
    state = _state()
    out = agent.CallOutputStream(_Conn(), state)
    out._in_rate = agent.INPUT_SAMPLE_RATE

    await out(BidiResponseStartEvent(response_id="r1"))
    await out(_transcript("Sure, I'll hold."))
    pcm = bytes(agent.frames.AUDIO_FRAME_BYTES * 3)
    await out(_audio_delta(pcm))
    await out(BidiResponseStopEvent(response_id="r1"))

    assert len(out._buf) == len(pcm)
    assert bytes(out._pending_audio) == b""
