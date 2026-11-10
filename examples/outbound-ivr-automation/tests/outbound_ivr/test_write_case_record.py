"""`write_case_record`: `OUTBOUND_IVR_ARTIFACTS_BUCKET` set writes to S3 keyed by `case_id`
(`cases/<case_id>/<call>.json`), not `pa_reference`, because the dashboard API
(lambdas/api/handler.py) lists `cases/<case_id>/` to find a case's calls. Without the
bucket set, it falls back to `runs/cases/<pa_reference>-<call>.json` on local disk, and
so does a failed S3 write; `handle_call`'s teardown survives the write failing at all.
"""

import asyncio
import json

import boto3
import pytest
from moto import mock_aws

from outbound_ivr import intake
from outbound_ivr.voice import agent


class _Log:
    def write(self, *a, **k) -> None:
        pass


def _state(case_id: str | None) -> agent.CallState:
    state = agent.CallState(call_uuid="call-1", mode="intake", log=_Log(), case_id=case_id)
    state.record = intake.new_record()
    state.record["pa_reference"] = "PA-2026-88431"
    return state


def test_without_a_bucket_writes_under_runs_cases_by_pa_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "RUNS", tmp_path)
    monkeypatch.delenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", raising=False)
    state = _state(case_id="some-other-case-id")

    agent.write_case_record(state)

    out = tmp_path / "cases" / "PA-2026-88431-call-1.json"
    assert out.exists()
    assert json.loads(out.read_text())["pa_reference"] == "PA-2026-88431"


def test_with_a_bucket_writes_to_s3_keyed_by_case_id(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", "outbound-ivr-artifacts-test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    state = _state(case_id="CASE-REAL-123")

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket="outbound-ivr-artifacts-test"
        )
        agent.write_case_record(state)

        body = (
            boto3.client("s3", region_name="us-east-1")
            .get_object(
                Bucket="outbound-ivr-artifacts-test", Key="cases/CASE-REAL-123/call-1.json"
            )["Body"]
            .read()
        )

    assert json.loads(body)["pa_reference"] == "PA-2026-88431"


def test_with_a_bucket_falls_back_to_pa_reference_without_a_case_id(monkeypatch):
    """No case_id resolved (shouldn't normally happen -- transports always set one --
    but the key must still be sane rather than literally 'cases/None/...')."""
    monkeypatch.setenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", "outbound-ivr-artifacts-test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    state = _state(case_id=None)

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket="outbound-ivr-artifacts-test"
        )
        agent.write_case_record(state)

        keys = [
            o["Key"]
            for o in boto3.client("s3", region_name="us-east-1")
            .list_objects_v2(Bucket="outbound-ivr-artifacts-test")
            .get("Contents", [])
        ]

    assert keys == ["cases/PA-2026-88431/call-1.json"]


def test_without_a_bucket_logs_no_warning(tmp_path, monkeypatch):
    """No OUTBOUND_IVR_ARTIFACTS_BUCKET at all must keep working exactly as before, with no
    spurious "bucket not set" warning on every call."""
    from structlog.testing import capture_logs

    monkeypatch.setattr(agent, "RUNS", tmp_path)
    monkeypatch.delenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", raising=False)
    state = _state(case_id="CASE-REAL-123")

    with capture_logs() as cap_logs:
        agent.write_case_record(state)

    out = tmp_path / "cases" / "PA-2026-88431-call-1.json"
    assert out.exists()
    assert not any("OUTBOUND_IVR_ARTIFACTS_BUCKET not set" in str(log) for log in cap_logs)


class _FailingS3Store:
    """Stands in for `S3ArtifactStore` when the PutObject itself fails (throttling, an
    expired role session, a bucket policy change): every `put` raises."""

    def put(self, key, body, content_type=None):
        raise RuntimeError("simulated S3 PutObject failure")


def test_an_s3_failure_falls_back_to_the_local_store_and_logs_both_locations(tmp_path, monkeypatch):
    """A failed S3 put must not lose the case record: the same body lands on local disk,
    the one place left to recover it from, and the log names both locations."""
    from structlog.testing import capture_logs

    monkeypatch.setattr(agent, "RUNS", tmp_path)
    monkeypatch.setattr(agent, "S3ArtifactStore", _FailingS3Store)
    monkeypatch.setenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", "outbound-ivr-artifacts-test")
    state = _state(case_id="CASE-REAL-123")

    with capture_logs() as cap_logs:
        agent.write_case_record(state)  # must not raise

    out = tmp_path / "cases" / "PA-2026-88431-call-1.json"
    assert json.loads(out.read_text())["pa_reference"] == "PA-2026-88431"
    failed = [log for log in cap_logs if log["event"] == "case_record_s3_write_failed"]
    assert len(failed) == 1
    assert failed[0]["call_id"] == "call-1"
    assert failed[0]["s3_key"] == "cases/CASE-REAL-123/call-1.json"
    assert failed[0]["fallback_location"] == str(out)


class _TeardownTransport:
    """A minimal `Transport`-shaped fake for `handle_call`: one intake phase that ends
    immediately, so the call goes straight to its `finally` teardown."""

    def __init__(self) -> None:
        self.mode = "intake"
        self.case_id = "CASE-REAL-123"
        self.peer = "fake-teardown"
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def handshake(self) -> str:
        return "call-teardown-1"

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
        self.closed = True

    def send_event(self, record) -> None:
        pass


async def test_handle_call_teardown_still_runs_when_writing_the_case_record_fails(
    tmp_path, monkeypatch
):
    """Even if the case record write fails outright (S3 *and* the local fallback), the
    rest of `handle_call`'s teardown -- the recording upload and the call log -- must
    still run."""
    pytest.importorskip("strands")

    monkeypatch.setattr(agent, "RUNS", tmp_path)
    monkeypatch.setattr(agent, "LEVELS_ENABLED", False)
    monkeypatch.delenv("OUTBOUND_IVR_EVENTS_TABLE", raising=False)

    async def fake_run_intake_phase(conn, state) -> None:
        state.finish_phase(None)

    def failing_write_case_record(state) -> None:
        raise RuntimeError("simulated case record failure")

    uploaded: list[str] = []
    monkeypatch.setattr(agent, "run_intake_phase", fake_run_intake_phase)
    monkeypatch.setattr(agent, "write_case_record", failing_write_case_record)
    monkeypatch.setattr(agent, "upload_recording", lambda t: uploaded.append(t.peer))
    transport = _TeardownTransport()

    await asyncio.wait_for(agent.handle_call(transport), timeout=5)  # must not raise

    assert transport.closed
    assert uploaded == ["fake-teardown"]
    events = tmp_path / "events" / "call-teardown-1.jsonl"
    assert "ended" in [json.loads(line)["type"] for line in events.read_text().splitlines()]
