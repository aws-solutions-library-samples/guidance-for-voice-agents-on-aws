"""`transfer_to_team` (docs/escalation-handoff.md): the warm handoff tool, `TeamDialer`,
and `handle_call` ending with reason "handed off" without hanging up the SIP leg --
all against fakes, no live calls.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("strands")

from outbound_ivr import intake
from outbound_ivr.voice import agent
from outbound_ivr.voice.backend import HandoffResult, LocalBackend, TeamDialer


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, type_: str, **fields) -> None:
        self.events.append((type_, fields))


class _FakeTeamDialer(TeamDialer):
    def __init__(
        self, *, joined: bool, identity: str | None = "team-abc", briefing: str | None = None
    ) -> None:
        self.joined = joined
        self.identity = identity
        self.briefing = briefing
        self.calls: list[dict] = []
        self.cleared: list[str] = []

    async def request_handoff(self, *, room, summary, reason, items) -> HandoffResult:
        self.calls.append({"room": room, "summary": summary, "reason": reason, "items": items})
        return HandoffResult(
            joined=self.joined,
            identity=self.identity if self.joined else None,
            briefing=self.briefing if self.joined else None,
        )

    async def clear_handoff(self, room: str) -> None:
        self.cleared.append(room)


def _state(mode: str = "intake") -> agent.CallState:
    state = agent.CallState(call_uuid="call-1", mode=mode, log=_Log())
    state.record = intake.new_record()
    state.case_id = "PA-2026-88431"
    state.backend = LocalBackend(state.record, state.case_id)
    return state


# --------------------------------------------------------------------------
# The tool always exists.
# --------------------------------------------------------------------------


def test_transfer_to_team_is_always_added(monkeypatch):
    state = _state()
    tools = agent.make_intake_tools(state)
    assert [t.tool_name for t in tools] == [
        "record_item",
        "escalate",
        "complete",
        "transfer_to_team",
    ]


# --------------------------------------------------------------------------
# transfer_to_team itself
# --------------------------------------------------------------------------


def _tool(state: agent.CallState, monkeypatch):
    *_, transfer_to_team = agent.make_intake_tools(state)
    return transfer_to_team


def test_transfer_to_team_requires_a_reason(monkeypatch):
    state = _state()
    state.team_dialer = _FakeTeamDialer(joined=True)
    transfer_to_team = _tool(state, monkeypatch)

    result = asyncio.run(transfer_to_team(reason="  ", summary="needs PT dates"))

    assert result == "A reason is required to bring in a colleague."
    assert state.team_dialer.calls == []
    assert state.handed_off is False
    assert not state.handoff_joined.is_set()


def test_transfer_to_team_requires_a_summary(monkeypatch):
    state = _state()
    state.team_dialer = _FakeTeamDialer(joined=True)
    transfer_to_team = _tool(state, monkeypatch)

    result = asyncio.run(transfer_to_team(reason="needs a supervisor", summary=""))

    assert result == "A summary is required to bring in a colleague."
    assert state.team_dialer.calls == []


def test_warm_introduction_instruction_carries_the_summary_near_verbatim():
    """Found live (2026-10-17): asking Nova Sonic to "paraphrase" after the tool
    returned was unreliable -- it sometimes said nothing at all. The tool result now
    composes the exact sentence (summary spliced into a fixed carrier) and tells the
    model to say it word for word, instead of leaving composition to the model."""
    result = agent._warm_introduction_instruction("PT dates still missing for Jordan Ellis.")

    assert "PT dates still missing for Jordan Ellis." in result
    assert "word for word" in result
    assert "paraphrase" not in result.lower()
    assert "I'll let you two take it from here," in result


def test_transfer_to_team_warm_intro_once_a_colleague_joins(monkeypatch):
    state = _state()
    state.record["items"]["physical_therapy"] = {"value": "6 weeks", "confirmed": True}
    dialer = _FakeTeamDialer(joined=True, identity="team-abc")
    state.team_dialer = dialer
    transfer_to_team = _tool(state, monkeypatch)

    result = asyncio.run(
        transfer_to_team(reason="front desk needs a supervisor", summary="PT dates still missing")
    )

    assert "introduce" in result.lower()
    assert "stop talking" in result.lower()
    assert dialer.calls == [
        {
            "room": "call-1",
            "summary": "PT dates still missing",
            "reason": "front desk needs a supervisor",
            "items": state.record["items"],
        }
    ]
    assert state.handed_off is True
    assert state.handoff_joined.is_set()
    assert state.closing_since is not None
    assert state.closing_next_phase == "handed_off"
    assert state.record["outcome"]["type"] == "handed_off"
    assert state.record["outcome"]["joined_identity"] == "team-abc"
    assert state.record["handoff_requests"][0]["reason"] == "front desk needs a supervisor"
    event_types = [t for t, _ in state.log.events]
    assert event_types == ["handoff_requested", "handoff_ringing", "handoff_joined", "handoff"]
    assert state.log.events[-2][1] == {"identity": "team-abc"}
    assert state.log.events[-1][1] == {
        "outcome": "handed_off",
        "reason": "front desk needs a supervisor",
        "summary": "PT dates still missing",
        "identity": "team-abc",
    }


def test_transfer_to_team_uses_the_dialers_briefing_over_the_models_summary(monkeypatch):
    """`AwsTeamDialer`'s direct mode (docs/escalation-handoff.md's AWS "direct"
    variant) hands back its own code-composed, name-free `briefing` instead of trusting
    the model's own `summary` -- the warm introduction must say that, not the model's
    summary, whenever a dialer sets it."""
    state = _state()
    dialer = _FakeTeamDialer(
        joined=True, identity="team-abc", briefing="This is case PA-2026-88431. Needs a supervisor."
    )
    state.team_dialer = dialer
    transfer_to_team = _tool(state, monkeypatch)

    result = asyncio.run(
        transfer_to_team(reason="needs a supervisor", summary="PT dates missing for Jordan Ellis")
    )

    assert "This is case PA-2026-88431. Needs a supervisor." in result
    assert "Jordan Ellis" not in result


def test_transfer_to_team_refuses_a_second_call_in_the_same_call(monkeypatch):
    """One handoff per call: a second transfer_to_team call (a fake model calling it
    twice, e.g. after "no answer" or just repeating itself) must not say the pre-tool
    line again or ring the team a second time -- it gets told to wait quietly instead."""
    state = _state()
    dialer = _FakeTeamDialer(joined=False)
    state.team_dialer = dialer
    transfer_to_team = _tool(state, monkeypatch)

    first = asyncio.run(transfer_to_team(reason="needs a supervisor", summary="PT dates missing"))
    assert "no one was available" in first.lower()
    assert len(dialer.calls) == 1

    second = asyncio.run(transfer_to_team(reason="needs a supervisor", summary="PT dates missing"))

    assert len(dialer.calls) == 1  # not rung again
    assert "already asked" in second.lower()
    assert "one moment" in second.lower()
    assert "do not call this tool again" in second.lower()


def test_transfer_to_team_no_answer_falls_back_to_escalate(monkeypatch):
    state = _state()
    dialer = _FakeTeamDialer(joined=False)
    state.team_dialer = dialer
    transfer_to_team = _tool(state, monkeypatch)

    result = asyncio.run(transfer_to_team(reason="needs a supervisor", summary="PT dates missing"))

    assert "no one was available" in result.lower()
    assert "escalate" in result.lower()
    assert state.handed_off is False
    assert not state.handoff_joined.is_set()
    assert state.closing_since is None
    assert "outcome" not in state.record or state.record["outcome"] is None
    event_types = [t for t, _ in state.log.events]
    assert event_types == ["handoff_requested", "handoff_ringing", "handoff_failed"]
    assert state.log.events[-1][1] == {"reason": "no answer"}


# --------------------------------------------------------------------------
# LocalBackend.transfer_to_team: the business-rule record of the request.
# --------------------------------------------------------------------------


async def test_local_backend_transfer_to_team_records_bookkeeping_and_business_outcome():
    record = intake.new_record()
    backend = LocalBackend(record, case_id=record["pa_reference"])

    entry = await backend.transfer_to_team(
        case_id=record["pa_reference"],
        call="call-1",
        reason="needs a supervisor",
        summary="PT dates missing",
    )

    assert entry["reason"] == "needs a supervisor"
    assert record["handoff_requests"] == [entry]
    # Doesn't touch record["outcome"]: not a final outcome yet (that's set once a
    # colleague actually joins -- see the agent.py tool tests above).
    assert record["outcome"] is None


async def test_local_backend_mark_handoff_joined_sets_the_case_outcome():
    record = intake.new_record()
    backend = LocalBackend(record, case_id=record["pa_reference"])
    await backend.transfer_to_team(
        case_id=record["pa_reference"],
        call="call-1",
        reason="needs a supervisor",
        summary="PT dates missing",
    )

    outcome = await backend.mark_handoff_joined(
        case_id=record["pa_reference"],
        call="call-1",
        reason="needs a supervisor",
        summary="PT dates missing",
        identity="team-abc",
    )

    assert outcome["type"] == "handed_off"
    assert outcome["joined_identity"] == "team-abc"
    assert record["outcome"] == outcome
    # The business-rule case store (the same path record_item/escalate/complete go
    # through) also carries the final "handed_off" outcome, not just the local record.
    case = backend._store.get_case(record["pa_reference"])
    assert case["outcome"]["type"] == "handed_off"
    assert case["outcome"]["joined_identity"] == "team-abc"
    assert case["status"] == "handed off"


def test_team_dialer_base_clear_handoff_is_a_no_op():
    """Any `TeamDialer` that doesn't ring something external gets a harmless no-op
    for free."""

    class _Bare(TeamDialer):
        async def request_handoff(self, *, room, summary, reason, items):
            raise NotImplementedError

    assert asyncio.run(_Bare().clear_handoff("call-1")) is None


# --------------------------------------------------------------------------
# handle_call: ends with reason "handed off", no hang_up(), stays until the room empties.
# --------------------------------------------------------------------------


class _HandoffTransport:
    """A fake `Transport` whose `stop_publishing()` simulates the call ending shortly
    after Outbound IVR goes silent -- the far end and the team member finish up and leave,
    which real teardown reaches via `_end_call()`/`frames()` ending; this fake just ends
    `frames()` directly."""

    def __init__(self) -> None:
        self.mode = "intake"
        self.case_id = None
        self.peer = "fake-handoff"
        self.hang_up_called = False
        self.stop_publishing_called = False
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def handshake(self) -> str:
        return "call-1"

    async def frames(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def send_audio(self, pcm: bytes) -> None:
        pass

    async def press_key(self, digit: str) -> None:
        pass

    async def hang_up(self) -> None:
        self.hang_up_called = True

    async def close(self) -> None:
        self.closed = True

    def send_event(self, record) -> None:
        pass

    async def stop_publishing(self) -> None:
        self.stop_publishing_called = True
        self._queue.put_nowait(None)  # the room empties right after Outbound IVR goes silent

    async def wait_for_team_join(self, timeout: float) -> str | None:
        return "team-abc"


async def test_handle_call_ends_handed_off_without_hanging_up(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "RUNS", tmp_path)
    monkeypatch.setattr(agent, "LEVELS_ENABLED", False)

    dialer = _FakeTeamDialer(joined=True)

    async def _fake_run_intake_phase(conn, state) -> None:
        # Stand in for transfer_to_team already having run inside a real phase.
        state.team_dialer = dialer
        state.handed_off = True
        state.next_phase = "handed_off"

    monkeypatch.setattr(agent, "run_intake_phase", _fake_run_intake_phase)

    transport = _HandoffTransport()
    await asyncio.wait_for(agent.handle_call(transport), timeout=5)

    assert transport.hang_up_called is False
    assert transport.stop_publishing_called is True
    assert transport.closed is True
    # The call is over (the team and the far end have both left): a Evidence team card
    # that's still showing "connected" for this room must be told to drop to idle.
    assert dialer.cleared == ["call-1"]


async def test_handle_call_signals_the_callers_handed_off_event(monkeypatch, tmp_path):
    """`handle_call(handed_off=...)` is the event `transfer_to_team` sets: it is how
    `agentcore/livekit_app.py` knows to switch to the post-handoff call ceiling."""
    monkeypatch.setattr(agent, "RUNS", tmp_path)
    monkeypatch.setattr(agent, "LEVELS_ENABLED", False)
    seen: list[asyncio.Event] = []

    async def _fake_run_intake_phase(conn, state) -> None:
        seen.append(state.handoff_joined)
        state.handed_off = True
        state.handoff_joined.set()
        state.next_phase = "handed_off"

    monkeypatch.setattr(agent, "run_intake_phase", _fake_run_intake_phase)

    handed_off = asyncio.Event()
    await asyncio.wait_for(agent.handle_call(_HandoffTransport(), handed_off=handed_off), timeout=5)

    assert seen == [handed_off]
    assert handed_off.is_set()


async def test_handle_call_clears_the_evidence_card_on_a_plain_hangup(monkeypatch, tmp_path):
    """Even when `transfer_to_team` was never tried on this call (no handoff state to
    clear), ending the call must not raise just because `team_dialer` is wired -- and
    when it *was* tried and failed/joined earlier, the same end-of-call cleanup must
    still run."""
    monkeypatch.setattr(agent, "RUNS", tmp_path)
    monkeypatch.setattr(agent, "LEVELS_ENABLED", False)

    dialer = _FakeTeamDialer(joined=False)

    async def _fake_run_intake_phase(conn, state) -> None:
        state.team_dialer = dialer
        state.next_phase = None
        state.hangup.set()

    monkeypatch.setattr(agent, "run_intake_phase", _fake_run_intake_phase)

    transport = _HandoffTransport()
    await asyncio.wait_for(agent.handle_call(transport), timeout=5)

    assert dialer.cleared == ["call-1"]


# --------------------------------------------------------------------------
# escalate (docs/escalation-handoff.md, part 1): refuses it until transfer_to_team has
# been tried on this call and failed, or there is no team_dialer to try it with at all.
# --------------------------------------------------------------------------


def test_escalate_refused_before_transfer_to_team_is_tried(monkeypatch):
    state = _state()
    state.team_dialer = _FakeTeamDialer(joined=True)
    _, escalate, _, _ = agent.make_intake_tools(state)

    result = asyncio.run(escalate(reason="front desk won't share chart details"))

    assert result == "First bring in a colleague with transfer_to_team."
    assert state.record["outcome"] is None


def test_escalate_allowed_after_transfer_to_team_fails(monkeypatch):
    state = _state()
    state.team_dialer = _FakeTeamDialer(joined=False)
    _, escalate, _, transfer_to_team = agent.make_intake_tools(state)

    first = asyncio.run(transfer_to_team(reason="needs a supervisor", summary="PT dates missing"))
    assert "no one was available" in first.lower()
    assert state.transfer_to_team_failed is True

    result = asyncio.run(escalate(reason="front desk won't share chart details"))

    assert result != "First bring in a colleague with transfer_to_team."
    assert state.record["outcome"]["type"] == "escalated"


def test_escalate_allowed_when_there_is_no_team_dialer(monkeypatch):
    """An "impossible" handoff -- no team_dialer for this call at all -- does not block
    escalate: there is nothing for the model to try."""
    state = _state()
    state.team_dialer = None
    _, escalate, _, _ = agent.make_intake_tools(state)

    result = asyncio.run(escalate(reason="front desk won't share chart details"))

    assert result != "First bring in a colleague with transfer_to_team."
    assert state.record["outcome"]["type"] == "escalated"
