"""The pushback regression (RESULTS.md, "transfer_to_team never fires"): when the office
side goes silent on a hold and never says another word, nothing ever happens for the
model to react to, so the "two exchanges in a row" / "more than twice in a row" stall
rules in `prompts.PRIOR_AUTH_PROMPT` / `prompts.TRANSFER_TO_TEAM_NOTE` can never trigger.
`_intake_hold_watchdog` fixes that by sending the model a text turn once nothing new has
been heard for `INTAKE_HOLD_NUDGE_SECONDS`, repeating for as long as the silence
continues, so a hold that just never resolves still counts as more than one cycle.
"""

import asyncio

import pytest

pytest.importorskip("strands.bidi")

from strands.bidi.models.model import BidiModel
from strands.bidi.types import BidiMessage

from outbound_ivr.voice import agent


def _texts_sent(sent: list) -> list[str]:
    """`model.send()` is also used to forward audio deltas (`AudioDelta`), not just text
    turns: pick out only the text blocks, which is what the watchdog's nudge is."""
    out = []
    for item in sent:
        if isinstance(item, BidiMessage):
            out.extend(block.text for block in item.content if hasattr(block, "text"))
    return out


class _Conn:
    async def send_audio(self, pcm: bytes) -> None:
        pass


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def write(self, type_: str, **fields) -> None:
        self.events.append({"type": type_, **fields})


def _state(mode: str = "full_call") -> agent.CallState:
    return agent.CallState(call_uuid="c", mode=mode, log=_RecordingLog())


class _FakeModel(BidiModel):
    """Stays silent (never produces another event) until the test ends the phase.
    Captures every `send()`, so the test can see whether, and how many times, the
    watchdog nudged the model.
    """

    def __init__(self) -> None:
        self.sent: list = []
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
        pass

    async def send(self, content) -> None:
        self.sent.append(content)

    async def receive(self):
        await asyncio.Event().wait()  # never produces another event
        yield None


async def test_intake_hold_watchdog_nudges_after_silence(monkeypatch):
    """A hold that never gets a second word from the office still gets nudged."""
    monkeypatch.setattr(agent, "INTAKE_HOLD_NUDGE_SECONDS", 0.05)
    state = _state()
    model = _FakeModel()
    monkeypatch.setattr(agent, "BedrockNovaSonicModel", lambda *a, **k: model)

    async def _end_once_nudged():
        while not _texts_sent(model.sent):
            await asyncio.sleep(0.01)
        state.finish_phase(None)

    ender = asyncio.create_task(_end_once_nudged())
    await asyncio.wait_for(agent.run_intake_phase(_Conn(), state), timeout=5)
    await ender

    assert _texts_sent(model.sent) == [agent.INTAKE_HOLD_NUDGE_TEXT]


async def test_intake_hold_watchdog_nudges_repeatedly_while_silence_continues(monkeypatch):
    """A hold that goes on and on keeps getting nudged, not just once."""
    monkeypatch.setattr(agent, "INTAKE_HOLD_NUDGE_SECONDS", 0.05)
    state = _state()
    model = _FakeModel()
    monkeypatch.setattr(agent, "BedrockNovaSonicModel", lambda *a, **k: model)

    async def _end_once_nudged_twice():
        while len(_texts_sent(model.sent)) < 2:
            await asyncio.sleep(0.01)
        state.finish_phase(None)

    ender = asyncio.create_task(_end_once_nudged_twice())
    await asyncio.wait_for(agent.run_intake_phase(_Conn(), state), timeout=5)
    await ender

    texts = _texts_sent(model.sent)
    assert len(texts) >= 2
    assert all(text == agent.INTAKE_HOLD_NUDGE_TEXT for text in texts)


async def test_intake_hold_watchdog_resets_on_new_transcript(monkeypatch):
    """Something new being heard resets the silence clock: no nudge if the office keeps
    talking, even past what would otherwise have been the nudge deadline."""
    monkeypatch.setattr(agent, "INTAKE_HOLD_NUDGE_SECONDS", 0.1)
    state = _state()
    model = _FakeModel()
    monkeypatch.setattr(agent, "BedrockNovaSonicModel", lambda *a, **k: model)

    async def _keep_hearing_things_then_end():
        for _ in range(5):
            await asyncio.sleep(0.05)
            state.hear("still here")
        state.finish_phase(None)

    ender = asyncio.create_task(_keep_hearing_things_then_end())
    await asyncio.wait_for(agent.run_intake_phase(_Conn(), state), timeout=5)
    await ender

    assert _texts_sent(model.sent) == []


async def test_intake_hold_watchdog_does_not_run_during_navigate(monkeypatch):
    """The nudge mechanism is intake-only: navigate keeps its own existing watchdog
    (`_navigate_watchdog`, which restarts the phase), not this one."""
    monkeypatch.setattr(agent, "NAVIGATE_WATCHDOG_SECONDS", 0.05)
    monkeypatch.setattr(agent, "PHASE_RETRIES", 0)
    state = _state(mode="full_call")
    model = _FakeModel()
    monkeypatch.setattr(agent, "BedrockNovaSonicModel", lambda *a, **k: model)

    await asyncio.wait_for(agent.run_navigate_phase(_Conn(), state), timeout=5)

    assert _texts_sent(model.sent) == []
