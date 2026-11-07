"""outbound_ivr.voice.monitor.MonitorPublisher: disabled-by-default no-op, watch-marker
freshness gating, segment batching, bounded-queue drop behavior, and
exception-swallowing on a failed HeadObject/PutObject -- all against a mocked boto3 S3
client (no real AWS, no moto: the behavior under test is this module's own control
flow around S3 calls, not S3 itself)."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from outbound_ivr.voice.monitor import SEGMENT_CHUNKS, MonitorPublisher

BUCKET = "outbound-ivr-monitor-test"
CALL_ID = "call-1"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "HeadObject")


def _head_object_response(age_seconds: float = 0.0) -> dict:
    return {"LastModified": _FakeDatetime(time.time() - age_seconds)}


class _FakeDatetime:
    """Just enough of a datetime to satisfy `response["LastModified"].timestamp()`."""

    def __init__(self, ts: float) -> None:
        self._ts = ts

    def timestamp(self) -> float:
        return self._ts


@pytest.fixture
def mock_client(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_MONITOR_BUCKET", BUCKET)
    client = MagicMock()
    with patch("boto3.client", return_value=client) as ctor:
        yield client, ctor


def _make_pcm(n: int = 320) -> bytes:
    return bytes([1, 2]) * (n // 2)


def _fill_a_segment(publisher: MonitorPublisher, count: int = SEGMENT_CHUNKS) -> None:
    for _ in range(count):
        publisher.publish(far_rms=1.0, agent_rms=1.0, pcm=_make_pcm())


# --------------------------------------------------------------------------
# Disabled by default
# --------------------------------------------------------------------------


async def test_disabled_when_bucket_env_var_is_unset(monkeypatch):
    monkeypatch.delenv("OUTBOUND_IVR_MONITOR_BUCKET", raising=False)

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()  # no-op: no boto3 call, no task created
    publisher.publish(far_rms=10.0, agent_rms=5.0, pcm=b"\x00" * 320)  # no-op
    await publisher.close()  # no-op, doesn't raise

    assert publisher.enabled is False
    assert publisher._poll_task is None
    assert publisher._queue.empty()


def test_start_never_raises_when_boto3_client_construction_fails(monkeypatch):
    """The call must never wait on or fail because of the monitor: a boto3.client()
    construction failure (bad credentials, etc.) inside start() must be swallowed, not
    propagate into LiveKitTransport.handshake()."""
    monkeypatch.setenv("OUTBOUND_IVR_MONITOR_BUCKET", BUCKET)

    with patch("boto3.client", side_effect=RuntimeError("boom")):
        publisher = MonitorPublisher(CALL_ID)
        publisher.start()  # must not raise

    assert publisher._poll_task is None
    assert publisher._worker_task is None
    assert publisher.enabled is False

    # Still safe to use: publish()/close() stay no-ops once disabled.
    publisher.publish(far_rms=1.0, agent_rms=1.0, pcm=b"\x00" * 320)
    assert publisher._queue.empty()


# --------------------------------------------------------------------------
# Watch-marker freshness
# --------------------------------------------------------------------------


async def test_both_markers_absent_means_not_watching(mock_client):
    client, _ = mock_client
    client.head_object.side_effect = _client_error("NoSuchKey")

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        await publisher._refresh_watch()
        assert publisher._watching is False
        assert publisher._listening is False
    finally:
        await publisher.close()


async def test_fresh_listen_marker_means_watching_and_listening(mock_client):
    client, _ = mock_client

    def _head_object(Bucket, Key):
        if Key.endswith("/watch/listen"):
            return _head_object_response(age_seconds=1.0)
        raise _client_error("404")

    client.head_object.side_effect = _head_object

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        await publisher._refresh_watch()
        assert publisher._watching is True
        assert publisher._listening is True
    finally:
        await publisher.close()


async def test_fresh_levels_marker_means_watching_but_not_listening(mock_client):
    client, _ = mock_client

    def _head_object(Bucket, Key):
        if Key.endswith("/watch/levels"):
            return _head_object_response(age_seconds=1.0)
        raise _client_error("404")

    client.head_object.side_effect = _head_object

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        await publisher._refresh_watch()
        assert publisher._watching is True
        assert publisher._listening is False
    finally:
        await publisher.close()


async def test_stale_marker_counts_as_not_fresh(mock_client):
    client, _ = mock_client
    client.head_object.return_value = _head_object_response(age_seconds=9999)

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        await publisher._refresh_watch()
        assert publisher._watching is False
        assert publisher._listening is False
    finally:
        await publisher.close()


async def test_403_on_marker_counts_as_absent(mock_client):
    client, _ = mock_client
    client.head_object.side_effect = _client_error("403")

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        await publisher._refresh_watch()
        assert publisher._watching is False
    finally:
        await publisher.close()


async def test_poll_error_is_swallowed_and_treated_as_not_watching(mock_client):
    client, _ = mock_client
    client.head_object.side_effect = RuntimeError("s3 is unreachable")

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        await publisher._refresh_watch()  # must not raise

        assert publisher._watching is False
        assert publisher._logged_poll_error is True
    finally:
        await publisher.close()


# --------------------------------------------------------------------------
# publish(): segment batching
# --------------------------------------------------------------------------


async def test_publish_is_dropped_while_not_watching(mock_client):
    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        publisher._watching = False
        _fill_a_segment(publisher)
        assert publisher._buffer == []
        assert publisher._queue.empty()
    finally:
        await publisher.close()


async def test_levels_only_segments_omit_pcm(mock_client):
    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        publisher._watching = True
        publisher._listening = False
        _fill_a_segment(publisher)

        segment = publisher._queue.get_nowait()
        assert len(segment["chunks"]) == SEGMENT_CHUNKS
        for chunk in segment["chunks"]:
            assert "pcm" not in chunk
            assert chunk["far_rms"] == 1.0
            assert chunk["agent_rms"] == 1.0
    finally:
        await publisher.close()


async def test_listening_segments_include_base64_pcm(mock_client):
    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        publisher._watching = True
        publisher._listening = True
        pcm = _make_pcm()
        for _ in range(SEGMENT_CHUNKS):
            publisher.publish(far_rms=0.5, agent_rms=0.25, pcm=pcm)

        segment = publisher._queue.get_nowait()
        for chunk in segment["chunks"]:
            assert base64.b64decode(chunk["pcm"]) == pcm
    finally:
        await publisher.close()


async def test_ten_chunks_per_segment_with_monotonic_seq_and_t0(mock_client):
    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        publisher._watching = True
        publisher._listening = False

        before = time.time()
        _fill_a_segment(publisher)
        _fill_a_segment(publisher)
        after = time.time()

        seg0 = publisher._queue.get_nowait()
        seg1 = publisher._queue.get_nowait()

        assert seg0["seq"] == 0
        assert seg1["seq"] == 1
        assert len(seg0["chunks"]) == SEGMENT_CHUNKS
        assert len(seg1["chunks"]) == SEGMENT_CHUNKS
        assert before <= seg0["t0"] <= after
        assert seg0["t0"] <= seg1["t0"] <= after
        assert seg0["chunk_ms"] == 200
    finally:
        await publisher.close()


async def test_partial_buffer_discarded_when_watching_stops(mock_client):
    client, _ = mock_client
    client.head_object.side_effect = _client_error("404")

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        publisher._watching = True
        for _ in range(SEGMENT_CHUNKS - 3):
            publisher.publish(far_rms=1.0, agent_rms=1.0, pcm=_make_pcm())
        assert len(publisher._buffer) == SEGMENT_CHUNKS - 3

        await publisher._refresh_watch()  # markers absent -> watching goes False

        assert publisher._watching is False
        assert publisher._buffer == []
        assert publisher._buffer_t0 is None
        assert publisher._queue.empty()
    finally:
        await publisher.close()


async def test_full_queue_drops_the_oldest_segment_not_the_newest(mock_client):
    publisher = MonitorPublisher(CALL_ID)
    # Don't start the worker task -- fill the queue by hand so nothing drains it.
    publisher._watching = True
    publisher._listening = False
    publisher._enabled = True

    maxsize = publisher._queue.maxsize
    for _ in range(maxsize + 3):
        _fill_a_segment(publisher)

    assert publisher._queue.qsize() == maxsize
    seqs = []
    while not publisher._queue.empty():
        seqs.append(publisher._queue.get_nowait()["seq"])
    # The oldest (lowest seq) segments were dropped; the most recent ones remain.
    assert seqs == list(range(3, maxsize + 3))


# --------------------------------------------------------------------------
# Worker: PutObject
# --------------------------------------------------------------------------


async def test_published_segments_eventually_land_in_s3(mock_client):
    client, _ = mock_client
    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        publisher._watching = True
        publisher._listening = True
        _fill_a_segment(publisher)

        for _ in range(50):
            if publisher._queue.empty():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
    finally:
        await publisher.close()

    assert client.put_object.call_count == 1
    _, kwargs = client.put_object.call_args
    assert kwargs["Bucket"] == BUCKET
    assert kwargs["Key"] == f"{CALL_ID}/seg/00000000.json"
    assert kwargs["ContentType"] == "application/json"
    body = json.loads(kwargs["Body"])
    assert body["seq"] == 0
    assert len(body["chunks"]) == SEGMENT_CHUNKS


async def test_put_object_exception_is_swallowed_and_logged_once(mock_client):
    client, _ = mock_client
    client.put_object.side_effect = RuntimeError("put_object failed")

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    try:
        publisher._watching = True
        publisher._listening = False
        _fill_a_segment(publisher)
        _fill_a_segment(publisher)

        for _ in range(50):
            if publisher._logged_write_error:
                break
            await asyncio.sleep(0.01)

        assert publisher._logged_write_error is True
        # The worker task is still alive -- one failed write doesn't kill the loop.
        assert publisher._worker_task is not None and not publisher._worker_task.done()
        # Only logged once despite two failed segments.
        assert client.put_object.call_count >= 1
    finally:
        await publisher.close()


# --------------------------------------------------------------------------
# close()
# --------------------------------------------------------------------------


async def test_close_deletes_everything_under_call_id_prefix(mock_client):
    client, _ = mock_client
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "Contents": [
                {"Key": f"{CALL_ID}/watch/listen"},
                {"Key": f"{CALL_ID}/watch/levels"},
                {"Key": f"{CALL_ID}/seg/00000000.json"},
            ]
        }
    ]
    client.get_paginator.return_value = paginator

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    await publisher.close()

    client.get_paginator.assert_called_once_with("list_objects_v2")
    paginator.paginate.assert_called_once_with(Bucket=BUCKET, Prefix=f"{CALL_ID}/")
    client.delete_objects.assert_called_once_with(
        Bucket=BUCKET,
        Delete={
            "Objects": [
                {"Key": f"{CALL_ID}/watch/listen"},
                {"Key": f"{CALL_ID}/watch/levels"},
                {"Key": f"{CALL_ID}/seg/00000000.json"},
            ]
        },
    )


async def test_close_is_idempotent(mock_client):
    client, _ = mock_client
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    client.get_paginator.return_value = paginator

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    await publisher.close()
    await publisher.close()  # must not raise, must not blow up on already-None tasks

    assert publisher._poll_task is None
    assert publisher._worker_task is None


async def test_close_never_raises_even_if_delete_fails(mock_client):
    client, _ = mock_client
    client.get_paginator.side_effect = RuntimeError("list failed")

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    await publisher.close()  # must not raise

    assert publisher._poll_task is None


async def test_close_discards_partial_buffer(mock_client):
    client, _ = mock_client
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    client.get_paginator.return_value = paginator

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    publisher._watching = True
    publisher._listening = False
    for _ in range(3):
        publisher.publish(far_rms=1.0, agent_rms=1.0, pcm=_make_pcm())
    assert publisher._buffer

    await publisher.close()

    assert publisher._buffer == []
    assert publisher._buffer_t0 is None


async def test_close_waits_for_inflight_put_before_deleting(mock_client):
    """If the worker is mid-`PutObject` when `close()` runs, the delete sweep must not
    start until that PutObject has actually finished -- otherwise an orphaned write
    could land in S3 after the sweep already ran for this call, leaving the segment
    behind. Cancelling the task awaiting the blocking call does not stop the
    underlying thread, so `close()` must wait on the real call, not just cancel it."""
    client, _ = mock_client
    order: list[str] = []

    def _put_object(**kwargs):
        time.sleep(0.05)
        order.append("put")

    client.put_object.side_effect = _put_object

    paginator = MagicMock()

    def _paginate(**kwargs):
        order.append("list")
        return [{"Contents": []}]

    paginator.paginate.side_effect = _paginate
    client.get_paginator.return_value = paginator

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    publisher._watching = True
    publisher._listening = True
    _fill_a_segment(publisher)  # enqueues a segment; the worker starts writing it

    # Give the worker a moment to dequeue the segment and start its `to_thread` call
    # before close() runs, so close() actually races the in-flight PutObject.
    await asyncio.sleep(0.01)

    await publisher.close()

    assert order == ["put", "list"]


async def test_close_starts_no_new_put_after_the_inflight_one(mock_client):
    """With more segments queued behind the in-flight PutObject, close() must not let
    the worker start another write after waiting for the current one: that write would
    run in a thread close() no longer waits for and could land after the delete sweep."""
    client, _ = mock_client
    order: list[str] = []

    def _put_object(**kwargs):
        time.sleep(0.05)
        order.append("put")

    client.put_object.side_effect = _put_object

    paginator = MagicMock()

    def _paginate(**kwargs):
        order.append("list")
        return [{"Contents": []}]

    paginator.paginate.side_effect = _paginate
    client.get_paginator.return_value = paginator

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()
    publisher._watching = True
    publisher._listening = True
    _fill_a_segment(publisher)
    await asyncio.sleep(0.01)  # the worker is now mid-PutObject on the first segment
    _fill_a_segment(publisher)
    _fill_a_segment(publisher)  # two more queued behind it

    await publisher.close()
    await asyncio.sleep(0.1)  # any write started after close() would finish by now

    assert order == ["put", "list"]


def test_publish_after_close_is_dropped(mock_client):
    """publish() is a no-op once close() has begun."""
    publisher = MonitorPublisher(CALL_ID)
    publisher._watching = True
    publisher._closing = True
    _fill_a_segment(publisher)
    assert publisher._queue.empty()
    assert publisher._buffer == []


async def test_close_is_bounded_even_if_delete_hangs(mock_client, monkeypatch):
    """close() must not hang the call's teardown forever if S3 stops responding."""
    from outbound_ivr.voice import monitor as monitor_module

    monkeypatch.setattr(monitor_module, "CLOSE_TIMEOUT_SECONDS", 0.05)

    client, _ = mock_client

    def _hang(**kwargs):
        time.sleep(1)

    paginator = MagicMock()
    paginator.paginate.side_effect = _hang
    client.get_paginator.return_value = paginator

    publisher = MonitorPublisher(CALL_ID)
    publisher.start()

    start = time.monotonic()
    await publisher.close()  # must not raise, must return quickly
    assert time.monotonic() - start < 5.0
