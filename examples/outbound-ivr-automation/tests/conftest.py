"""Shared test fixtures.

Two guards against tests writing into the real `runs/` tree instead of `tmp_path`:

1. `_default_recording_dir` redirects `outbound_ivr.voice.livekit_transport.recording_path`'s
   default directory to this test's own `tmp_path`, so a test that builds a
   `LiveKitTransport` without explicitly wiring a recording directory (the pattern
   `tests/test_livekit_transport.py`'s own `_wire_recording` helper uses) still never
   touches the real `runs/recordings/` -- this is what let `runs/recordings/outbound-ivr-call-1
   .wav` leak into the repo's own working tree from a test run.
2. `_fail_if_a_test_writes_under_runs` fails the test outright if any file under the real
   `runs/` directory is created or modified while it runs, as a backstop for any other
   path (an `EventLog`, `write_case_record`, a `CallRecorder` built some other way) that
   isn't already redirected to `tmp_path`.

A third, unrelated-to-`runs/` guard:

3. `_reset_livekit_api_secret_cache` resets `outbound_ivr.voice.livekit_calls`'s module-global
   `_cached_api_secret` before and after every test. `resolve_api_secret()` caches its
   result for the life of the process by design (Secrets Manager is a network call that
   never needs to change mid-run in production) -- without this reset, one test's
   `LIVEKIT_API_SECRET_ARN`/`LIVEKIT_API_SECRET` monkeypatching would leak into whatever
   test (in this file or another) happens to call `resolve_api_secret()`/
   `make_admin_client()` next.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Runtime/Lambda code reads os.environ["AWS_REGION"] with no fallback (Lambda and
# AgentCore Runtime always set it); set a default here, before any
# `outbound_ivr` module import, so the test suite runs without AWS credentials or an
# ambient AWS_REGION. Only a default -- a developer's real AWS_REGION still wins, and
# individual tests that care about a specific region still monkeypatch it themselves.
os.environ.setdefault("AWS_REGION", "us-east-1")

from outbound_ivr.voice import livekit_calls
from outbound_ivr.voice import livekit_transport as lkt

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"


@pytest.fixture(autouse=True)
def _default_recording_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(lkt, "recording_path", lambda call_id: tmp_path / f"{call_id}.wav")


@pytest.fixture(autouse=True)
def _reset_livekit_api_secret_cache():
    livekit_calls._cached_api_secret = None
    yield
    livekit_calls._cached_api_secret = None


def _snapshot() -> dict[Path, float]:
    if not RUNS.is_dir():
        return {}
    return {p: p.stat().st_mtime for p in RUNS.rglob("*") if p.is_file()}


@pytest.fixture(autouse=True)
def _fail_if_a_test_writes_under_runs():
    before = _snapshot()
    yield
    after = _snapshot()
    new_or_changed = {p: after[p] for p in after if p not in before or after[p] != before[p]}
    if new_or_changed:
        paths = ", ".join(str(p.relative_to(ROOT)) for p in new_or_changed)
        pytest.fail(f"test wrote under the repo's own runs/ directory instead of tmp_path: {paths}")
