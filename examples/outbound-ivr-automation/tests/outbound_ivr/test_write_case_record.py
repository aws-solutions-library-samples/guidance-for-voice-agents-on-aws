"""`write_case_record`: `OUTBOUND_IVR_ARTIFACTS_BUCKET` set writes to S3 keyed by `case_id`
(`cases/<case_id>/<call>.json`), not `pa_reference`, because the dashboard API
(lambdas/api/handler.py) lists `cases/<case_id>/` to find a case's calls. Without the
bucket set, it falls back to `runs/cases/<pa_reference>-<call>.json` on local disk.
"""

import json

import boto3
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
