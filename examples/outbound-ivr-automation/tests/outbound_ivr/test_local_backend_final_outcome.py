"""`LocalBackend` must not overwrite a final outcome: once a case is escalated or
complete, a second `escalate`/`complete` is refused by `outbound_ivr.tools.business`'s
"already has a final outcome" guard, and the call's own record -- the one that becomes
the case record JSON in S3 -- must keep the first outcome too, not just the case store.
The model is told the outcome is already final instead of being told it succeeded.
"""

from __future__ import annotations

import asyncio

import pytest

from outbound_ivr import intake
from outbound_ivr.tools import business
from outbound_ivr.voice.backend import LocalBackend


def _backend() -> tuple[dict, LocalBackend]:
    record = intake.new_record()
    return record, LocalBackend(record, case_id=record["pa_reference"])


def _collect_every_item(record: dict, backend: LocalBackend) -> None:
    for key in intake.item_keys(record):
        asyncio.run(
            backend.record_item(
                case_id=None, call="call-1", key=key, value="sent by fax", confirmed=True
            )
        )


def test_a_second_escalate_after_complete_leaves_the_outcome_unchanged():
    record, backend = _backend()
    _collect_every_item(record, backend)
    asyncio.run(backend.complete(case_id=None, call="call-1", summary="all sent"))
    assert record["outcome"] == {"type": "complete", "summary": "all sent"}

    result = asyncio.run(
        backend.escalate(case_id=None, call="call-1", reason="changed its mind", follow_up=None)
    )

    assert result == {"type": "refused", "reason": business.NO_FINAL_OUTCOME}
    assert record["outcome"] == {"type": "complete", "summary": "all sent"}


def test_a_second_complete_after_escalate_leaves_the_outcome_unchanged():
    record, backend = _backend()
    asyncio.run(
        backend.escalate(case_id=None, call="call-1", reason="front desk refused", follow_up=None)
    )
    assert record["outcome"] == {"type": "escalated", "reason": "front desk refused"}

    result = asyncio.run(backend.complete(case_id=None, call="call-1", summary="all sent"))

    assert result == {"type": "refused", "reason": business.NO_FINAL_OUTCOME}
    assert record["outcome"] == {"type": "escalated", "reason": "front desk refused"}


def test_transfer_to_team_after_a_final_outcome_records_no_handoff_request():
    record, backend = _backend()
    asyncio.run(
        backend.escalate(case_id=None, call="call-1", reason="front desk refused", follow_up=None)
    )

    result = asyncio.run(
        backend.transfer_to_team(
            case_id=None, call="call-1", reason="needs a supervisor", summary="PT dates"
        )
    )

    assert result == {"type": "refused", "reason": business.NO_FINAL_OUTCOME}
    assert "handoff_requests" not in record
    assert record["outcome"] == {"type": "escalated", "reason": "front desk refused"}


def test_escalate_after_a_transfer_request_is_still_allowed():
    """`transfer_to_team`'s `handoff_requested` is not final (see `business.
    transfer_to_team`): the no-answer fallback to `escalate` must still land."""
    record, backend = _backend()
    asyncio.run(
        backend.transfer_to_team(
            case_id=None, call="call-1", reason="needs a supervisor", summary="PT dates"
        )
    )

    result = asyncio.run(
        backend.escalate(case_id=None, call="call-1", reason="no one answered", follow_up=None)
    )

    assert result == {"type": "escalated", "reason": "no one answered"}
    assert record["outcome"] == result


def test_the_complete_tool_tells_the_model_the_outcome_is_already_final():
    pytest.importorskip("strands")
    from outbound_ivr.voice import agent

    class _Log:
        def __init__(self) -> None:
            self.events: list[str] = []

        def write(self, type_: str, **fields) -> None:
            self.events.append(type_)

    state = agent.CallState(call_uuid="call-1", mode="intake", log=_Log())
    state.record = intake.new_record()
    state.case_id = state.record["pa_reference"]
    state.backend = LocalBackend(state.record, state.case_id)
    _, escalate, complete, _ = agent.make_intake_tools(state)
    asyncio.run(escalate(reason="front desk refused"))
    state.log.events.clear()

    result = asyncio.run(complete(summary="all sent"))

    assert result == business.NO_FINAL_OUTCOME
    assert "handoff" not in state.log.events
    assert state.record["outcome"]["type"] == "escalated"
