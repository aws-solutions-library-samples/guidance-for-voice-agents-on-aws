"""`agentcore/livekit_app.py`: the case's status around one call. The dashboard claims
the case as "queued"; the Runtime marks it "calling" once the office is dialed, and every
way a call can end moves it out of "calling" again, so a case is never left undialable.

Against fakes of `outbound_ivr.voice.livekit_calls` and `agent.handle_call`, and moto's
DynamoDB (so the condition expressions are really evaluated) -- no LiveKit, no AWS.
"""

from __future__ import annotations

import asyncio

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from structlog.testing import capture_logs

pytest.importorskip("bedrock_agentcore")
pytest.importorskip("strands")

from agentcore import livekit_app

CALL_ID = "00000000-0000-4000-8000-0000000000a1"
NEWER_CALL_ID = "00000000-0000-4000-8000-0000000000b2"
CASE_ID = "CASE-1"


@pytest.fixture
def tables(monkeypatch):
    """A cases table holding CASE_ID exactly as the dashboard's Start call leaves it
    ("queued", `current_call` = CALL_ID), and an empty events table."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", "cases")
    monkeypatch.setenv("OUTBOUND_IVR_EVENTS_TABLE", "events")
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        cases = dynamodb.create_table(
            TableName="cases",
            AttributeDefinitions=[{"AttributeName": "case_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "case_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        events = dynamodb.create_table(
            TableName="events",
            AttributeDefinitions=[
                {"AttributeName": "call", "AttributeType": "S"},
                {"AttributeName": "seq", "AttributeType": "N"},
            ],
            KeySchema=[
                {"AttributeName": "call", "KeyType": "HASH"},
                {"AttributeName": "seq", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        cases.put_item(
            Item={
                "case_id": CASE_ID,
                "status": "queued",
                "current_call": CALL_ID,
                "attempts": 0,
                "last_call_started_at": "2026-11-10T00:00:00+00:00",
            }
        )
        yield {"cases": cases, "events": events}


def _case(tables) -> dict:
    return tables["cases"].get_item(Key={"case_id": CASE_ID}, ConsistentRead=True)["Item"]


def _set_case(tables, expression: str, values: dict | None = None) -> None:
    kwargs = {"Key": {"case_id": CASE_ID}, "UpdateExpression": expression}
    if "#status" in expression:
        kwargs["ExpressionAttributeNames"] = {"#status": "status"}
    if values:
        kwargs["ExpressionAttributeValues"] = values
    tables["cases"].update_item(**kwargs)


def _handoff(tables, outcome: str = "complete", call_id: str = CALL_ID) -> None:
    tables["events"].put_item(
        Item={"call": call_id, "seq": 5, "type": "handoff", "outcome": outcome, "summary": "ok"}
    )


async def _until_calling(tables, timeout: float = 2.0) -> str:
    """Poll (as the dashboard does) until the Runtime has marked the case "calling"."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        status = (await asyncio.to_thread(_case, tables))["status"]
        if status == "calling":
            return status
        await asyncio.sleep(0.01)
    return (await asyncio.to_thread(_case, tables))["status"]


class _FakeLkapi:
    async def aclose(self) -> None:
        return None


def _wire_call(monkeypatch, handle_call, *, dial=None):
    """Fake every LiveKit step `_run_call` takes, and the AgentCore busy bookkeeping and
    span attributes `_run_call_tracked` adds, so only the case writes are real."""
    calls = livekit_app.livekit_calls

    async def wait_for_agent_join(client, call_id, proc, identity, timeout):
        await asyncio.sleep(0)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(calls, "make_admin_client", lambda: _FakeLkapi())
    monkeypatch.setattr(calls, "resolve_api_secret", lambda: "secret")
    monkeypatch.setattr(calls, "create_room", noop)
    monkeypatch.setattr(calls, "wait_for_agent_join", wait_for_agent_join)
    monkeypatch.setattr(calls, "dial_office", dial or noop)
    monkeypatch.setattr(calls, "delete_room", noop)
    monkeypatch.setattr(livekit_app.agent, "handle_call", handle_call)
    monkeypatch.setattr(livekit_app.app, "add_async_task", lambda name, metadata=None: 1)
    monkeypatch.setattr(livekit_app.app, "complete_async_task", lambda task_id: True)
    monkeypatch.setattr(livekit_app, "_evaluation_attributes", lambda call_id, case_id: {})
    monkeypatch.setenv("OUTBOUND_IVR_OFFICE_NUMBER", "+15555550101")


def _assert_handed_back(case: dict, status: str = "pending") -> None:
    assert case["status"] == status
    assert "current_call" not in case
    assert case["last_call"] == CALL_ID
    assert case["attempts"] == 1


# --------------------------------------------------------------------------
# The happy path: queued -> calling -> complete.
# --------------------------------------------------------------------------


async def test_the_case_is_calling_during_the_call_and_closed_out_after(tables, monkeypatch):
    seen = {}

    async def handle_call(transport):
        seen["status"] = await _until_calling(tables)
        await asyncio.to_thread(_handoff, tables, "complete")

    _wire_call(monkeypatch, handle_call)
    await livekit_app._run_call_tracked(CALL_ID, CASE_ID)

    assert seen["status"] == "calling"
    case = _case(tables)
    _assert_handed_back(case, "complete")
    assert case["outcome"] == {"type": "complete", "summary": "ok"}


# --------------------------------------------------------------------------
# Every other way a call ends leaves the case out of "calling".
# --------------------------------------------------------------------------


async def test_a_call_that_ends_without_an_outcome_goes_back_to_pending(tables, monkeypatch):
    seen = {}

    async def handle_call(transport):
        seen["status"] = await _until_calling(tables)

    _wire_call(monkeypatch, handle_call)
    await livekit_app._run_call_tracked(CALL_ID, CASE_ID)

    assert seen["status"] == "calling"
    _assert_handed_back(_case(tables))


async def test_a_call_that_ends_abnormally_goes_back_to_pending(tables, monkeypatch):
    seen = {}

    async def handle_call(transport):
        seen["status"] = await _until_calling(tables)
        raise RuntimeError("media stream dropped")

    _wire_call(monkeypatch, handle_call)
    await livekit_app._run_call_tracked(CALL_ID, CASE_ID)

    assert seen["status"] == "calling"
    _assert_handed_back(_case(tables))


async def test_a_call_cut_off_by_the_call_ceiling_goes_back_to_pending(tables, monkeypatch):
    seen = {}

    async def handle_call(transport):
        seen["status"] = await _until_calling(tables)
        await asyncio.sleep(30)

    _wire_call(monkeypatch, handle_call)
    monkeypatch.setattr(livekit_app.livekit_calls, "CALL_TIMEOUT_S", 0.5)
    await asyncio.wait_for(livekit_app._run_call_tracked(CALL_ID, CASE_ID), timeout=5)

    assert seen["status"] == "calling"
    _assert_handed_back(_case(tables))


async def test_a_cancelled_call_goes_back_to_pending(tables, monkeypatch):
    calling = asyncio.Event()

    async def handle_call(transport):
        await _until_calling(tables)
        calling.set()
        await asyncio.sleep(30)

    _wire_call(monkeypatch, handle_call)
    task = asyncio.create_task(livekit_app._run_call_tracked(CALL_ID, CASE_ID))
    await asyncio.wait_for(calling.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    _assert_handed_back(_case(tables))


async def test_a_call_cancelled_while_dialing_stops_the_agent_and_goes_back_to_pending(
    tables, monkeypatch
):
    """Cancelled before the call ceiling's wait starts: the agent task is stopped too,
    instead of the cleanup waiting on it, and the case is still handed back."""
    dialing = asyncio.Event()
    agent_cancelled = asyncio.Event()

    async def handle_call(transport):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            agent_cancelled.set()
            raise

    async def slow_dial(*_a, **_k):
        dialing.set()
        await asyncio.sleep(30)

    _wire_call(monkeypatch, handle_call, dial=slow_dial)
    task = asyncio.create_task(livekit_app._run_call_tracked(CALL_ID, CASE_ID))
    await asyncio.wait_for(dialing.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert agent_cancelled.is_set()
    _assert_handed_back(_case(tables))


async def test_a_call_whose_dial_fails_is_never_calling_and_goes_back_to_pending(
    tables, monkeypatch
):
    async def handle_call(transport):
        await asyncio.sleep(30)

    async def failing_dial(*_a, **_k):
        raise RuntimeError("no trunk named that")

    _wire_call(monkeypatch, handle_call, dial=failing_dial)
    marked = []
    monkeypatch.setattr(livekit_app, "_mark_case_calling", lambda *a: marked.append(a))
    await asyncio.wait_for(livekit_app._run_call_tracked(CALL_ID, CASE_ID), timeout=5)

    assert marked == []
    _assert_handed_back(_case(tables))


# --------------------------------------------------------------------------
# The outcome write failing: retried without the outcome map, never raised.
# --------------------------------------------------------------------------


class _FlakyCases:
    """Wraps the real cases table; the first `failures` outcome writes (the ones that
    `ADD attempts`) raise a throttling error before reaching DynamoDB."""

    def __init__(self, table, failures: int) -> None:
        self._table = table
        self.failures = failures
        self.outcome_writes: list[dict] = []

    def update_item(self, **kwargs):
        if "ADD attempts" in kwargs["UpdateExpression"]:
            self.outcome_writes.append(kwargs)
            if self.failures > 0:
                self.failures -= 1
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
                )
        return self._table.update_item(**kwargs)

    def __getattr__(self, name):
        return getattr(self._table, name)


def _flaky_cases(monkeypatch, tables, failures: int) -> _FlakyCases:
    flaky = _FlakyCases(tables["cases"], failures)
    real_resource = boto3.resource

    class _Resource:
        def __init__(self, *a, **k):
            self._real = real_resource(*a, **k)

        def Table(self, name):
            return flaky if name == "cases" else self._real.Table(name)

    monkeypatch.setattr(boto3, "resource", _Resource)
    return flaky


def test_a_failed_outcome_write_is_retried_without_the_outcome(tables, monkeypatch):
    _set_case(tables, "SET #status = :calling", {":calling": "calling"})
    _handoff(tables, "escalated")
    flaky = _flaky_cases(monkeypatch, tables, failures=1)

    livekit_app._record_case_outcome(CALL_ID, CASE_ID)

    first, second = flaky.outcome_writes
    assert ":outcome" in first["ExpressionAttributeValues"]
    assert ":outcome" not in second["ExpressionAttributeValues"]
    case = _case(tables)
    _assert_handed_back(case, "escalated")
    assert "outcome" not in case


def test_a_failed_outcome_write_without_an_outcome_is_retried(tables, monkeypatch):
    _set_case(tables, "SET #status = :calling", {":calling": "calling"})
    flaky = _flaky_cases(monkeypatch, tables, failures=1)

    livekit_app._record_case_outcome(CALL_ID, CASE_ID)

    assert len(flaky.outcome_writes) == 2
    _assert_handed_back(_case(tables))


def test_an_outcome_write_that_keeps_failing_never_raises_and_says_so(tables, monkeypatch):
    _set_case(tables, "SET #status = :calling", {":calling": "calling"})
    _handoff(tables, "complete")
    flaky = _flaky_cases(monkeypatch, tables, failures=2)

    with capture_logs() as logs:
        livekit_app._record_case_outcome(CALL_ID, CASE_ID)

    assert len(flaky.outcome_writes) == 2
    assert any(log["event"] == "case_outcome_record_failed_manual_reset_needed" for log in logs)


def test_an_unreadable_events_table_still_hands_the_case_back(tables, monkeypatch):
    _set_case(tables, "SET #status = :calling", {":calling": "calling"})
    monkeypatch.setenv("OUTBOUND_IVR_EVENTS_TABLE", "no-such-table")

    livekit_app._record_case_outcome(CALL_ID, CASE_ID)

    _assert_handed_back(_case(tables))


async def test_a_failed_calling_write_leaves_the_call_running_and_still_closes_out(
    tables, monkeypatch
):
    """Marking "calling" is best effort: if it fails the call goes ahead, the case
    simply stays "queued" until the outcome write closes it out."""
    seen = {}

    async def handle_call(transport):
        await asyncio.sleep(0.05)
        seen["status"] = (await asyncio.to_thread(_case, tables))["status"]
        await asyncio.to_thread(_handoff, tables, "complete")

    _wire_call(monkeypatch, handle_call)
    real_resource = boto3.resource

    class _Resource:
        def __init__(self, *a, **k):
            self._real = real_resource(*a, **k)

        def Table(self, name):
            table = self._real.Table(name)
            if name != "cases":
                return table

            class _NoCalling:
                def update_item(self, **kwargs):
                    if ":calling" in kwargs["ExpressionAttributeValues"]:
                        raise ClientError({"Error": {"Code": "InternalServerError"}}, "UpdateItem")
                    return table.update_item(**kwargs)

            return _NoCalling()

    monkeypatch.setattr(boto3, "resource", _Resource)
    await livekit_app._run_call_tracked(CALL_ID, CASE_ID)

    assert seen["status"] == "queued"
    _assert_handed_back(_case(tables), "complete")


# --------------------------------------------------------------------------
# A stale claim: the case was reset (and maybe redialed) while this call ran.
# --------------------------------------------------------------------------


def test_calling_is_only_marked_while_this_call_holds_the_claim(tables):
    livekit_app._mark_case_calling(CALL_ID, CASE_ID)
    assert _case(tables)["status"] == "calling"


def test_calling_is_not_marked_on_a_case_reset_before_the_dial(tables):
    _set_case(tables, "SET #status = :pending REMOVE current_call", {":pending": "pending"})
    livekit_app._mark_case_calling(CALL_ID, CASE_ID)
    case = _case(tables)
    assert case["status"] == "pending"
    assert "current_call" not in case


def test_calling_is_not_marked_on_a_case_a_newer_call_holds(tables):
    _set_case(tables, "SET current_call = :newer", {":newer": NEWER_CALL_ID})
    livekit_app._mark_case_calling(CALL_ID, CASE_ID)
    case = _case(tables)
    assert case["status"] == "queued"
    assert case["current_call"] == NEWER_CALL_ID


def test_marking_calling_never_raises(monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("no dynamodb")

    monkeypatch.setattr(boto3, "resource", broken)
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", "cases")
    livekit_app._mark_case_calling(CALL_ID, CASE_ID)


async def test_a_call_that_outlives_a_reset_and_a_new_dial_leaves_the_new_claim_alone(
    tables, monkeypatch
):
    async def handle_call(transport):
        await _until_calling(tables)
        # Mid-call, an operator resets the case and starts a fresh call.
        await asyncio.to_thread(
            _set_case,
            tables,
            "SET #status = :queued, current_call = :newer, attempts = :zero",
            {":queued": "queued", ":newer": NEWER_CALL_ID, ":zero": 0},
        )
        await asyncio.to_thread(_handoff, tables, "complete")

    _wire_call(monkeypatch, handle_call)
    with capture_logs() as logs:
        await livekit_app._run_call_tracked(CALL_ID, CASE_ID)

    case = _case(tables)
    assert case["status"] == "queued"
    assert case["current_call"] == NEWER_CALL_ID
    assert case["attempts"] == 0
    assert "outcome" not in case
    assert any(log["event"] == "case_outcome_skipped_claim_moved" for log in logs)


async def test_a_call_that_outlives_a_reset_still_records_its_outcome(tables, monkeypatch):
    """Reset with no new dial: `current_call` is gone, the case is "pending", and the
    call's outcome still lands when it ends, as it did before the "calling" status."""

    async def handle_call(transport):
        await _until_calling(tables)
        await asyncio.to_thread(
            _set_case,
            tables,
            "SET #status = :pending, attempts = :zero REMOVE current_call",
            {":pending": "pending", ":zero": 0},
        )
        await asyncio.to_thread(_handoff, tables, "complete")

    _wire_call(monkeypatch, handle_call)
    await livekit_app._run_call_tracked(CALL_ID, CASE_ID)

    _assert_handed_back(_case(tables), "complete")
