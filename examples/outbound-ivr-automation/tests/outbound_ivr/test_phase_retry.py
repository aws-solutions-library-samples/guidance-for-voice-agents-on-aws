"""A phase's Nova Sonic session can fail with a transient error; `run_navigate_phase`/
`run_intake_phase` reopen it with a fresh `BidiAgent`, same transport and call state, up
to `OUTBOUND_IVR_PHASE_RETRIES` times. Fake models that raise once then work, standing in for
Bedrock's `ModelStreamErrorException`.
"""

import asyncio

import pytest

pytest.importorskip("strands.bidi")

from strands.bidi.models.model import BidiModel

from outbound_ivr.voice import agent

ModelStreamErrorException = type("ModelStreamErrorException", (Exception,), {})


class _Conn:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def write(self, type_: str, **fields) -> None:
        self.events.append({"type": type_, **fields})

    def events_of(self, type_: str) -> list[dict]:
        return [e for e in self.events if e["type"] == type_]


def _state(mode: str = "full_call") -> agent.CallState:
    return agent.CallState(call_uuid="c", mode=mode, log=_RecordingLog())


class _FakeModel(BidiModel):
    """A Nova Sonic stand-in: either raises once from `receive()`, or stays silent (or
    answers a tool call) until the test ends the phase. Captures the system prompt and
    whatever `send()` is given, so tests can check resume context and "no second greeting".
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list = []
        self.stopped = False
        self.system_prompt: str | None = None

    @property
    def model_id(self) -> str:
        return "fake"

    def get_audio_config(self) -> dict:
        return {"input": {"sample_rate": 8000}, "output": {"sample_rate": 8000}}

    def get_connection_config(self) -> dict:
        return {}

    def get_config(self) -> dict:
        return {}

    def update_config(self, **kwargs) -> None:
        pass

    async def start(self, system_prompt=None, tools=None, messages=None, **kwargs) -> None:
        self.system_prompt = system_prompt

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, content) -> None:
        self.sent.append(content)

    async def receive(self):
        if self.fail:
            raise ModelStreamErrorException("The system encountered an unexpected error.")
        await asyncio.Event().wait()  # never produces another event; the test ends the phase
        yield None


def _model_sequence(monkeypatch: pytest.MonkeyPatch, models: list[BidiModel]) -> None:
    it = iter(models)
    monkeypatch.setattr(agent, "BedrockNovaSonicModel", lambda *a, **k: next(it))


async def test_navigate_phase_resumes_after_a_retryable_error(monkeypatch):
    state = _state()
    state.pressed_keys.append({"digit": "2", "option": "for providers, press two"})
    fail_model = _FakeModel(fail=True)
    work_model = _FakeModel(fail=False)
    _model_sequence(monkeypatch, [fail_model, work_model])

    async def _end_once_running():
        while work_model.system_prompt is None:
            await asyncio.sleep(0.01)
        state.finish_phase("intake")

    ender = asyncio.create_task(_end_once_running())
    await asyncio.wait_for(agent.run_navigate_phase(_Conn(), state), timeout=5)
    await ender

    # Resumed, not given up: moved on to intake, not ended the call.
    assert state.next_phase == "intake"
    # The resumed session was told what's already been pressed.
    assert "2 (for providers, press two)" in work_model.system_prompt
    assert "YOU WERE DISCONNECTED AND RECONNECTED" in work_model.system_prompt
    # A retry event was logged with the phase and attempt number.
    retries = state.log.events_of("retry")
    assert len(retries) == 1
    assert retries[0]["phase"] == "navigate" and retries[0]["attempt"] == 1
    assert "ModelStreamErrorException" in retries[0]["error"]


async def test_intake_phase_resumes_without_a_second_greeting_and_keeps_items(monkeypatch):
    state = _state(mode="intake")  # intake-only mode: no transfer-note branch to worry about
    # As if the first attempt already recorded one item before the stream died.
    state.record["items"]["physical_therapy"] = {
        "value": "eight weeks of PT",
        "confirmed": True,
        "at": "2026-01-01T00:00:00Z",
    }
    state.record["transcript"].append({"who": "far_end", "text": "yeah eight weeks of PT", "t": 0})
    state.record["transcript"].append({"who": "agent", "text": "Got it, thank you.", "t": 1})

    fail_model = _FakeModel(fail=True)
    work_model = _FakeModel(fail=False)
    _model_sequence(monkeypatch, [fail_model, work_model])

    async def _end_once_running():
        while work_model.system_prompt is None:
            await asyncio.sleep(0.01)
        state.finish_phase(None)

    ender = asyncio.create_task(_end_once_running())
    await asyncio.wait_for(agent.run_intake_phase(_Conn(), state), timeout=5)
    await ender

    # The item recorded before the failure is still there.
    assert state.record["items"]["physical_therapy"]["value"] == "eight weeks of PT"
    # Resume context mentions the recorded item and the prior conversation.
    assert "physical_therapy: eight weeks of PT" in work_model.system_prompt
    assert "yeah eight weeks of PT" in work_model.system_prompt
    # No second greeting: the resumed session is seeded with the "lost you" apology, not
    # the fixed opening line, and it is sent (not withheld like a fresh full_call wait).
    assert work_model.sent and "lost you for a second" in str(work_model.sent[0])
    assert "Hello, this is Outbound IVR" not in str(work_model.sent[0])

    retries = state.log.events_of("retry")
    assert len(retries) == 1 and retries[0]["phase"] == "intake" and retries[0]["attempt"] == 1


async def test_phase_gives_up_after_phase_retries_exhausted(monkeypatch):
    monkeypatch.setattr(agent, "PHASE_RETRIES", 1)
    state = _state(mode="intake")
    models = [_FakeModel(fail=True), _FakeModel(fail=True)]
    _model_sequence(monkeypatch, models)

    await asyncio.wait_for(agent.run_intake_phase(_Conn(), state), timeout=5)

    assert state.done.is_set()
    assert state.next_phase is None
    retries = state.log.events_of("retry")
    errors = state.log.events_of("error")
    assert len(retries) == 1  # one retry used, then gave up
    assert len(errors) == 1 and "ModelStreamErrorException" in errors[0]["text"]


async def test_non_retryable_error_ends_the_call_without_retrying(monkeypatch):
    state = _state(mode="intake")

    class _Boom(_FakeModel):
        async def receive(self):
            raise ValueError("not a Bedrock stream error")
            yield None  # pragma: no cover

    _model_sequence(monkeypatch, [_Boom(fail=False)])

    await asyncio.wait_for(agent.run_intake_phase(_Conn(), state), timeout=5)

    assert state.done.is_set() and state.next_phase is None
    assert not state.log.events_of("retry")
    errors = state.log.events_of("error")
    assert len(errors) == 1 and "ValueError" in errors[0]["text"]


def test_is_retryable_phase_error_matches_the_known_transient_errors():
    assert agent._is_retryable_phase_error(ModelStreamErrorException("x"))
    assert agent._is_retryable_phase_error(type("ThrottlingException", (Exception,), {})())
    assert agent._is_retryable_phase_error(ConnectionResetError())
    assert not agent._is_retryable_phase_error(ValueError("nope"))


def test_navigate_resume_note_lists_pressed_keys():
    state = _state()
    assert "none yet" in agent._navigate_resume_note(state)
    state.pressed_keys.append({"digit": "3", "option": "billing"})
    note = agent._navigate_resume_note(state)
    assert "3 (billing)" in note


def test_intake_resume_note_lists_items_and_conversation():
    state = _state(mode="intake")
    state.record["items"]["progress_note"] = {"value": "seen June 1", "confirmed": False}
    state.record["transcript"].append({"who": "far_end", "text": "hello there", "t": 0})
    note = agent._intake_resume_note(state)
    assert "progress_note: seen June 1 (not yet confirmed)" in note
    assert "far_end: hello there" in note
