"""Offline call through the real ECS pipeline with live Bedrock.

Runs ``scripts/pipeline_parity.py``'s capture (fake Daily transport, scripted
STT, the app's Deepgram SageMaker TTS over a fake BiDi client, real Bedrock)
and checks behaviour and the CloudWatch EMF schema.

``parity_emf_schema.json`` holds, per EMF record kind (turn, tool, call,
session), the namespace, dimension sets and metric name/unit pairs captured
from this call with pipecat-ai 0.0.108. Dashboards and alarms depend on these,
so a renamed metric, changed unit or changed dimension set fails the test.

Needs AWS credentials with Bedrock access (us-east-1); skipped otherwise.
Takes about 70 s. Deselect with ``-m "not integration"``.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent.parent / "scripts" / "pipeline_parity.py"
GOLDEN = HERE / "parity_emf_schema.json"


def _has_aws_credentials() -> bool:
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


@pytest.fixture(scope="module")
def capture(tmp_path_factory):
    if not _has_aws_credentials():
        pytest.skip("AWS credentials not available for live Bedrock")
    spec = importlib.util.spec_from_file_location("pipeline_parity", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path_factory.mktemp("parity") / "capture.json"
    import asyncio

    return asyncio.run(mod.run_capture(str(out))), mod


def test_call_flow_checks(capture):
    result, _ = capture
    checks = dict(result["checks"])
    turns = checks.pop("turns_completed")
    assert all(checks.values()), checks
    assert all(turns.values()), turns


def test_barge_in_stops_bot_audio(capture):
    result, _ = capture
    intr = result["interruption"]
    assert intr["frame_type"] is not None
    assert intr["audio_chunks_before_barge"] > 0
    # Output transport must drop queued bot audio once interrupted.
    assert intr["audio_chunks_written_after_interruption"] == 0, intr
    assert intr["latency_ms_from_user_speech"] < 500, intr


def test_tool_call_reaches_context(capture):
    result, _ = capture
    tool = result["tool"]
    assert tool["tool_call_in_context"], result["context"]
    assert tool["tool_result_in_context"], result["context"]
    assert tool["tool_metrics_emitted"]


def _schema_by_kind(records):
    """Per record kind: namespaces, dimension sets, and (metric, unit) pairs."""
    out = {}
    for rec in records:
        k = out.setdefault(rec["kind"], {"namespaces": set(), "dimension_sets": set(), "metrics": set()})
        for ms in rec["metric_sets"]:
            k["namespaces"].add(ms["namespace"])
            k["dimension_sets"].add(json.dumps(ms["dimensions"]))
            k["metrics"].update((name, unit) for name, unit in ms["metrics"])
    return out


def test_emf_schema_matches_baseline(capture):
    result, _ = capture
    golden = json.loads(GOLDEN.read_text())["kinds"]
    emitted = _schema_by_kind(result["emf"])
    assert set(emitted) == set(golden), (sorted(emitted), sorted(golden))
    for kind, got in emitted.items():
        want = golden[kind]
        assert got["namespaces"] == set(want["namespaces"]), kind
        assert got["dimension_sets"] == {json.dumps(d) for d in want["dimension_sets"]}, kind
        unknown = got["metrics"] - {tuple(m) for m in want["metrics"]}
        assert not unknown, f"{kind}: metric name/unit not emitted by the 0.0.108 baseline: {sorted(unknown)}"
    for rec in result["emf"]:
        missing = [k for k, ok in rec["dimension_values_present"].items() if not ok]
        assert not missing, f"{rec['kind']} record missing dimension values {missing}"


def test_clean_teardown(capture):
    result, _ = capture
    assert result["teardown"]["leftover_tasks"] == []
    assert result["teardown"]["warnings_during_teardown"] == []
