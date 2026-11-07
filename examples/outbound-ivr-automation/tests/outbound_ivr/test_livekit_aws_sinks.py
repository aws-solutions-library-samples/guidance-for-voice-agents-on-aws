"""Env-var-gated AWS sinks the Outbound IVR Runtime (agentcore/livekit_app.py) needs that
`write_case_record` didn't already cover: events -> DynamoDB (`_DynamoDBEventSink`,
`_combine_forwards`) and the finished recording -> S3 (`upload_recording`). Against
moto's mocked DynamoDB/S3 -- no real AWS -- plus one end-to-end `handle_call` test
confirming the DynamoDB sink is actually wired in and the local Mac demo's own file
sink is untouched by it.
"""

from __future__ import annotations

import asyncio
import json
import time

import boto3
import pytest
from moto import mock_aws

pytest.importorskip("strands")

from outbound_ivr.voice import agent

EVENTS_TABLE = "outbound-ivr-events-test"


def _create_events_table() -> None:
    """Same key shape as the real table (lambdas/api/README.md): partition key `call`,
    sort key `seq`."""
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=EVENTS_TABLE,
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


def _scan_sorted() -> list[dict]:
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(EVENTS_TABLE)
    items = table.scan()["Items"]
    return sorted(items, key=lambda i: int(i["seq"]))


@pytest.fixture(autouse=True)
def _pin_region(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")


# --------------------------------------------------------------------------
# _DynamoDBEventSink
# --------------------------------------------------------------------------


def test_dynamodb_event_sink_writes_the_handler_contract_item_shape():
    with mock_aws():
        _create_events_table()
        sink = agent._DynamoDBEventSink(EVENTS_TABLE)

        before = int(time.time())
        sink.write(
            {"ts": "2026-10-08T15:00:01.123Z", "call": "call-1", "type": "heard", "text": "hi"}
        )
        sink.write(
            {"ts": "2026-10-08T15:00:02.000Z", "call": "call-1", "type": "dtmf", "digit": "3"}
        )

        items = _scan_sorted()

    assert [i["seq"] for i in items] == [0, 1]
    first, second = items
    assert first["call"] == "call-1"
    assert first["type"] == "heard"
    assert first["text"] == "hi"
    assert first["t"] == "2026-10-08T15:00:01.123Z"
    assert "ts" not in first  # renamed, not duplicated
    assert int(first["expires_at"]) >= before + 6 * 24 * 3600  # ~7 days out
    assert second["type"] == "dtmf"
    assert second["digit"] == "3"


def test_dynamodb_event_sink_logs_and_swallows_a_write_failure():
    from structlog.testing import capture_logs

    with mock_aws(), capture_logs() as cap_logs:
        # No table created: PutItem fails with ResourceNotFoundException.
        sink = agent._DynamoDBEventSink(EVENTS_TABLE)

        sink.write({"ts": "t", "call": "call-1", "type": "heard", "text": "hi"})  # must not raise

    assert any(log.get("event") == "dynamodb_event_write_failed" for log in cap_logs)


# --------------------------------------------------------------------------
# _combine_forwards
# --------------------------------------------------------------------------


def test_combine_forwards_calls_every_forward():
    seen: list[tuple[str, dict]] = []
    forward = agent._combine_forwards(
        lambda r: seen.append(("a", r)), lambda r: seen.append(("b", r))
    )

    forward({"type": "heard"})

    assert seen == [("a", {"type": "heard"}), ("b", {"type": "heard"})]


def test_combine_forwards_skips_none_entries():
    seen = []
    forward = agent._combine_forwards(None, lambda r: seen.append(r), None)

    forward({"type": "x"})

    assert seen == [{"type": "x"}]


def test_combine_forwards_returns_the_lone_forward_unwrapped():
    def only(record):
        pass

    assert agent._combine_forwards(only, None) is only


def test_combine_forwards_returns_none_when_everything_is_none():
    assert agent._combine_forwards(None, None) is None


# --------------------------------------------------------------------------
# upload_recording
# --------------------------------------------------------------------------


class _Recorded:
    """A transport with a finished recording -- the shape `LiveKitTransport.
    recording_path` exposes."""

    def __init__(self, call_uuid: str, path):
        self.call_uuid = call_uuid
        self.recording_path = path


class _NoRecording:
    """A transport with no `recording_path` attribute at all."""

    def __init__(self, call_uuid: str):
        self.call_uuid = call_uuid


def test_upload_recording_puts_the_wav_at_the_handler_expected_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", "outbound-ivr-artifacts-test")
    wav = tmp_path / "call-1.wav"
    wav.write_bytes(b"RIFF....fake-wav-bytes")
    transport = _Recorded("call-1", wav)

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket="outbound-ivr-artifacts-test"
        )
        agent.upload_recording(transport)

        body = (
            boto3.client("s3", region_name="us-east-1")
            .get_object(Bucket="outbound-ivr-artifacts-test", Key="recordings/call-1.wav")["Body"]
            .read()
        )

    assert body == b"RIFF....fake-wav-bytes"


def test_upload_recording_does_nothing_without_a_bucket(tmp_path, monkeypatch):
    monkeypatch.delenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", raising=False)
    wav = tmp_path / "call-1.wav"
    wav.write_bytes(b"data")
    transport = _Recorded("call-1", wav)

    agent.upload_recording(transport)  # must not raise, and must not need AWS at all


def test_upload_recording_does_nothing_for_a_transport_with_no_recording(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", "outbound-ivr-artifacts-test")
    transport = _NoRecording("call-1")

    agent.upload_recording(transport)  # getattr(..., "recording_path", None) is None


def test_upload_recording_does_nothing_when_the_call_was_never_recorded(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", "outbound-ivr-artifacts-test")
    transport = _Recorded("call-1", None)  # room `record` setting was off

    agent.upload_recording(transport)  # must not try to read a None path


def test_upload_recording_tolerates_a_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", "outbound-ivr-artifacts-test")
    transport = _Recorded("call-1", tmp_path / "never-written.wav")

    agent.upload_recording(transport)  # must not raise just because start() never ran


# --------------------------------------------------------------------------
# End to end: handle_call wires the DynamoDB sink in when OUTBOUND_IVR_EVENTS_TABLE is set,
# alongside (not instead of) the local file sink the dashboard tails.
# --------------------------------------------------------------------------


class _FakeTransport:
    """A minimal `Transport`-shaped fake: one phase, ends immediately once that phase's
    `run_intake_phase` stub finishes."""

    def __init__(self) -> None:
        self.mode = "intake"
        self.case_id = None
        self.peer = "fake-sinks"
        self._queue: asyncio.Queue = asyncio.Queue()

    async def handshake(self) -> str:
        return "call-sinks-1"

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
        pass

    async def wait_for_team_join(self, timeout: float):
        return None

    async def close(self) -> None:
        pass

    def send_event(self, record) -> None:
        pass


async def test_handle_call_writes_events_to_dynamodb_and_still_writes_the_local_file(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(agent, "RUNS", tmp_path)
    monkeypatch.setattr(agent, "LEVELS_ENABLED", False)
    monkeypatch.setenv("OUTBOUND_IVR_EVENTS_TABLE", EVENTS_TABLE)

    async def fake_run_intake_phase(conn, state) -> None:
        state.finish_phase(None)

    monkeypatch.setattr(agent, "run_intake_phase", fake_run_intake_phase)

    with mock_aws():
        _create_events_table()
        await asyncio.wait_for(agent.handle_call(_FakeTransport()), timeout=5)
        items = _scan_sorted()

    types = [i["type"] for i in items]
    assert "ready" in types
    assert "ended" in types
    assert all("ts" not in i and "t" in i for i in items)

    local_file = tmp_path / "events" / "call-sinks-1.jsonl"
    assert local_file.exists()
    local_types = [
        json.loads(line)["type"] for line in local_file.read_text().splitlines() if line.strip()
    ]
    assert "ready" in local_types
    assert "ended" in local_types
