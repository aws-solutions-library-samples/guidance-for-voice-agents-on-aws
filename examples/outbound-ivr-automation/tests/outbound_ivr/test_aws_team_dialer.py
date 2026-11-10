"""`AwsTeamDialer` (docs/escalation-handoff.md, "AWS (spec only, not built now)" --
this builds it): the handoff-codes DynamoDB write, dialing the Evidence team through
the same outbound trunk, sending the code as DTMF, and waiting for the join -- against
moto's mocked DynamoDB and fakes of `outbound_ivr.voice.livekit_calls`'s own LiveKit calls, no
real LiveKit server and no real AWS.
"""

from __future__ import annotations

import time

import boto3
import pytest
from moto import mock_aws

from outbound_ivr.voice import livekit_calls
from outbound_ivr.voice.backend import (
    AwsTeamDialer,
    HandoffResult,
    _format_evidence_briefing,
    _format_items_collected,
)

HANDOFF_TABLE = "outbound-ivr-handoff-codes-test"
ROOM = "call-123"
CASE_ID = "CASE-REAL-1"


@pytest.fixture(autouse=True)
def _pin_region(monkeypatch):
    # AwsTeamDialer reads AWS_REGION (same as outbound_ivr.voice.agent's own REGION
    # constant), with a "us-east-1" fallback only when it's unset entirely -- pin it
    # explicitly so these tests don't depend on whatever AWS_REGION happens to be set
    # to in the ambient dev/CI environment.
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def _create_handoff_table() -> None:
    """Same key/TTL shape as `infrastructure/stacks/escalation_stack.py`'s real
    table: partition key `code`, TTL attribute `expires_at` (moto doesn't enforce TTL
    expiry itself, but this keeps the schema honest)."""
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=HANDOFF_TABLE,
        AttributeDefinitions=[{"AttributeName": "code", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "code", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _get_item(code: str) -> dict:
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(HANDOFF_TABLE)
    return table.get_item(Key={"code": code})["Item"]


def _scan_all() -> list[dict]:
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(HANDOFF_TABLE)
    return table.scan()["Items"]


class _Dialer:
    """Records `press_key`/`join_waiter` calls; built once per test with the table/
    evidence number/trunk name under test."""

    def __init__(self, **kw) -> None:
        self.pressed: list[str] = []
        self.join_waiter_calls: list[float] = []
        self.join_identity: str | None = "team-abc"

        async def press_key(digit: str) -> None:
            self.pressed.append(digit)

        async def join_waiter(timeout: float) -> str | None:
            self.join_waiter_calls.append(timeout)
            return self.join_identity

        kw.setdefault("evidence_number", "+15555550123")
        kw.setdefault("trunk_name", "outbound-ivr-chime-outbound")
        kw.setdefault("handoff_table", HANDOFF_TABLE)
        kw.setdefault("case_id", CASE_ID)
        self.dialer = AwsTeamDialer(join_waiter=join_waiter, press_key=press_key, **kw)


# --------------------------------------------------------------------------
# _format_items_collected
# --------------------------------------------------------------------------


def test_format_items_collected_renders_a_readable_line():
    items = {"physical_therapy": {"value": "6 weeks", "confirmed": True}}

    assert _format_items_collected(items) == "physical_therapy: 6 weeks"


def test_format_items_collected_handles_no_items():
    assert _format_items_collected({}) == "(nothing recorded yet)"


# --------------------------------------------------------------------------
# No evidence number configured: nothing is written or dialed.
# --------------------------------------------------------------------------


async def test_request_handoff_without_an_evidence_number_does_nothing(monkeypatch):
    monkeypatch.delenv("OUTBOUND_IVR_EVIDENCE_NUMBER", raising=False)
    harness = _Dialer(evidence_number=None)

    with mock_aws():
        _create_handoff_table()
        result = await harness.dialer.request_handoff(room=ROOM, summary="s", reason="r", items={})
        assert _scan_all() == []  # nothing written -- nothing to dial for

    assert result == HandoffResult(joined=False)
    assert harness.pressed == []
    assert harness.join_waiter_calls == []


# --------------------------------------------------------------------------
# The full happy path: write the code, dial, send DTMF, wait for the join.
# --------------------------------------------------------------------------


async def test_request_handoff_writes_the_code_dials_and_sends_dtmf(monkeypatch):
    harness = _Dialer()
    dial_calls: list[dict] = []

    def fake_make_admin_client():
        return "lkapi"

    async def fake_dial_office(client, call_id, sip_identity, *, extension, trunk_name):
        dial_calls.append(
            {
                "client": client,
                "call_id": call_id,
                "sip_identity": sip_identity,
                "extension": extension,
                "trunk_name": trunk_name,
            }
        )

    monkeypatch.setattr(livekit_calls, "make_admin_client", fake_make_admin_client)
    monkeypatch.setattr(livekit_calls, "dial_office", fake_dial_office)
    monkeypatch.setattr(livekit_calls, "team_identity", lambda room: f"team-fixed-{room}")

    before = int(time.time())
    with mock_aws():
        _create_handoff_table()
        result = await harness.dialer.request_handoff(
            room=ROOM,
            summary="PT dates still missing",
            reason="front desk needs a supervisor",
            items={"physical_therapy": {"value": "6 weeks", "confirmed": True}},
        )
        items = _scan_all()

    assert result == HandoffResult(joined=True, identity="team-abc")
    assert harness.join_waiter_calls == [30.0]  # TEAM_HANDOFF_WAIT_SECONDS's default

    assert dial_calls == [
        {
            "client": "lkapi",
            "call_id": ROOM,
            "sip_identity": f"team-fixed-{ROOM}",
            "extension": "+15555550123",
            "trunk_name": "outbound-ivr-chime-outbound",
        }
    ]

    assert len(items) == 1
    item = items[0]
    code = item["code"]
    assert len(code) == 6 and code.isdigit()
    assert item["case_id"] == CASE_ID
    assert item["items_collected"] == "physical_therapy: 6 weeks"
    assert item["escalation_reason"] == "front desk needs a supervisor"
    assert item["summary"] == "PT dates still missing"
    assert int(item["expires_at"]) >= before

    # The code, digit by digit, then '#' -- over the room's own DTMF path, same as the
    # navigate phase's press_key tool uses to work the office's menu.
    assert harness.pressed == [*code, "#"]


# --------------------------------------------------------------------------
# No answer: the SIP leg never connects, so nothing is sent and no one is waited for.
# --------------------------------------------------------------------------


async def test_request_handoff_returns_not_joined_when_dialing_fails(monkeypatch):
    harness = _Dialer()

    def fake_make_admin_client():
        return "lkapi"

    async def failing_dial_office(client, call_id, sip_identity, *, extension, trunk_name):
        raise RuntimeError("no outbound-ivr-chime-outbound trunk found")

    monkeypatch.setattr(livekit_calls, "make_admin_client", fake_make_admin_client)
    monkeypatch.setattr(livekit_calls, "dial_office", failing_dial_office)
    monkeypatch.setattr(livekit_calls, "team_identity", lambda room: f"team-{room}")

    with mock_aws():
        _create_handoff_table()
        result = await harness.dialer.request_handoff(room=ROOM, summary="s", reason="r", items={})
        # The code was written before dialing was attempted (so a human calling the
        # evidence line back in could still look it up), but nothing was ever sent.
        assert len(_scan_all()) == 1

    assert result == HandoffResult(joined=False)
    assert harness.pressed == []
    assert harness.join_waiter_calls == []


# --------------------------------------------------------------------------
# No one answers once a leg does connect: still returns not-joined, no DTMF skipped.
# --------------------------------------------------------------------------


async def test_request_handoff_returns_not_joined_when_no_one_joins(monkeypatch):
    harness = _Dialer()
    harness.join_identity = None

    def fake_make_admin_client():
        return "lkapi"

    async def fake_dial_office(client, call_id, sip_identity, *, extension, trunk_name):
        pass

    monkeypatch.setattr(livekit_calls, "make_admin_client", fake_make_admin_client)
    monkeypatch.setattr(livekit_calls, "dial_office", fake_dial_office)
    monkeypatch.setattr(livekit_calls, "team_identity", lambda room: f"team-{room}")

    with mock_aws():
        _create_handoff_table()
        result = await harness.dialer.request_handoff(room=ROOM, summary="s", reason="r", items={})

    assert result == HandoffResult(joined=False, identity=None)
    assert harness.pressed  # the code (and '#') were still sent -- the leg did answer


# --------------------------------------------------------------------------
# A code collision retries with a fresh one.
# --------------------------------------------------------------------------


async def test_write_handoff_code_retries_once_on_a_collision(monkeypatch):
    harness = _Dialer()
    seen = iter([111111, 111111, 222222])  # the first retried value collides too
    monkeypatch.setattr("outbound_ivr.voice.backend.secrets.randbelow", lambda n: next(seen))

    with mock_aws():
        _create_handoff_table()
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(HANDOFF_TABLE)
        table.put_item(Item={"code": "111111", "case_id": "someone-else's-call"})

        code = harness.dialer._write_handoff_code(items={}, reason="r", summary="s")

        assert code == "222222"
        assert _get_item("222222")["case_id"] == CASE_ID
        # The pre-existing row under the colliding code is untouched.
        assert _get_item("111111")["case_id"] == "someone-else's-call"


# --------------------------------------------------------------------------
# _format_evidence_briefing: what direct mode says, instead of the model's own summary.
# --------------------------------------------------------------------------


def test_format_evidence_briefing_names_the_case_reason_and_items():
    briefing = _format_evidence_briefing(
        case_id=CASE_ID,
        reason="front desk needs a supervisor",
        items={"physical_therapy": {"value": "6 weeks", "confirmed": True}},
    )

    assert briefing == (
        f"This is case {CASE_ID}. front desk needs a supervisor. So far we have: "
        "physical_therapy: 6 weeks."
    )


def test_format_evidence_briefing_without_a_case_id_or_items():
    assert _format_evidence_briefing(case_id=None, reason="r", items={}) == (
        "This is this case. r. So far we have: (nothing recorded yet)."
    )


def test_format_evidence_briefing_never_repeats_the_models_own_summary():
    """The whole point of a code-composed briefing (docs/escalation-handoff.md's AWS
    "direct" variant): a model-authored `summary` can name the patient (this demo
    case's own `patient_name`, "Jordan Ellis") the way the doc's own warm-intro example
    does -- `_format_evidence_briefing` only ever sees `reason` and `items`, so there is
    no argument through which a name could reach it."""
    briefing = _format_evidence_briefing(
        case_id=CASE_ID,
        reason="front desk needs a supervisor",
        items={"progress_note": {"value": "visit on Oct 1", "confirmed": True}},
    )

    assert "Jordan Ellis" not in briefing


# --------------------------------------------------------------------------
# OUTBOUND_IVR_EVIDENCE_MODE=direct: no handoff code, no DTMF -- dial, wait, brief.
# --------------------------------------------------------------------------


async def test_direct_mode_dials_and_skips_the_code_and_dtmf(monkeypatch):
    harness = _Dialer(evidence_mode="direct")
    dial_calls: list[dict] = []

    def fake_make_admin_client():
        return "lkapi"

    async def fake_dial_office(client, call_id, sip_identity, *, extension, trunk_name):
        dial_calls.append({"extension": extension, "trunk_name": trunk_name})

    monkeypatch.setattr(livekit_calls, "make_admin_client", fake_make_admin_client)
    monkeypatch.setattr(livekit_calls, "dial_office", fake_dial_office)
    monkeypatch.setattr(livekit_calls, "team_identity", lambda room: f"team-{room}")

    with mock_aws():
        _create_handoff_table()
        result = await harness.dialer.request_handoff(
            room=ROOM,
            summary="PT dates still missing for Jordan Ellis",
            reason="front desk needs a supervisor",
            items={"physical_therapy": {"value": "6 weeks", "confirmed": True}},
        )
        # Direct mode never writes a handoff code: there's nothing on the other end to
        # look it up.
        assert _scan_all() == []

    assert dial_calls == [
        {"extension": "+15555550123", "trunk_name": "outbound-ivr-chime-outbound"}
    ]
    assert harness.pressed == []  # no DTMF in direct mode
    assert harness.join_waiter_calls == [30.0]
    assert result.joined is True
    assert result.identity == "team-abc"
    assert result.briefing == (
        "This is case CASE-REAL-1. front desk needs a supervisor. So far we have: "
        "physical_therapy: 6 weeks."
    )
    # The dialer composes its own briefing from reason/items -- the model's own
    # (name-carrying) summary is never used for it.
    assert "Jordan Ellis" not in result.briefing


async def test_direct_mode_no_join_returns_not_joined_with_no_briefing(monkeypatch):
    harness = _Dialer(evidence_mode="direct")
    harness.join_identity = None

    async def fake_dial_office(client, call_id, sip_identity, *, extension, trunk_name):
        pass

    monkeypatch.setattr(livekit_calls, "make_admin_client", lambda: "lkapi")
    monkeypatch.setattr(livekit_calls, "dial_office", fake_dial_office)
    monkeypatch.setattr(livekit_calls, "team_identity", lambda room: f"team-{room}")

    result = await harness.dialer.request_handoff(room=ROOM, summary="s", reason="r", items={})

    assert result == HandoffResult(joined=False)
    assert harness.pressed == []


async def test_direct_mode_dial_failure_returns_not_joined_and_never_waits(monkeypatch):
    harness = _Dialer(evidence_mode="direct")

    async def failing_dial_office(client, call_id, sip_identity, *, extension, trunk_name):
        raise RuntimeError("no outbound-ivr-chime-outbound trunk found")

    monkeypatch.setattr(livekit_calls, "make_admin_client", lambda: "lkapi")
    monkeypatch.setattr(livekit_calls, "dial_office", failing_dial_office)
    monkeypatch.setattr(livekit_calls, "team_identity", lambda room: f"team-{room}")

    result = await harness.dialer.request_handoff(room=ROOM, summary="s", reason="r", items={})

    assert result == HandoffResult(joined=False)
    assert harness.join_waiter_calls == []


async def test_direct_mode_without_an_evidence_number_does_nothing(monkeypatch):
    monkeypatch.delenv("OUTBOUND_IVR_EVIDENCE_NUMBER", raising=False)
    harness = _Dialer(evidence_mode="direct", evidence_number=None)

    result = await harness.dialer.request_handoff(room=ROOM, summary="s", reason="r", items={})

    assert result == HandoffResult(joined=False)
    assert harness.join_waiter_calls == []


# --------------------------------------------------------------------------
# OUTBOUND_IVR_EVIDENCE_MODE itself: unset/unrecognized both mean "connect" (backward compatible).
# --------------------------------------------------------------------------


def test_evidence_mode_defaults_to_connect_when_unset(monkeypatch):
    monkeypatch.delenv("OUTBOUND_IVR_EVIDENCE_MODE", raising=False)

    harness = _Dialer()

    assert harness.dialer._evidence_mode == "connect"


def test_evidence_mode_falls_back_to_connect_for_an_unrecognized_value():
    harness = _Dialer(evidence_mode="carrier-pigeon")

    assert harness.dialer._evidence_mode == "connect"


def test_evidence_mode_is_case_insensitive():
    harness = _Dialer(evidence_mode="DIRECT")

    assert harness.dialer._evidence_mode == "direct"


def test_evidence_mode_reads_the_env_var_when_not_passed_explicitly(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_EVIDENCE_MODE", "direct")

    harness = _Dialer()

    assert harness.dialer._evidence_mode == "direct"


def test_evidence_mode_explicit_argument_wins_over_the_env_var(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_EVIDENCE_MODE", "direct")

    harness = _Dialer(evidence_mode="connect")

    assert harness.dialer._evidence_mode == "connect"
