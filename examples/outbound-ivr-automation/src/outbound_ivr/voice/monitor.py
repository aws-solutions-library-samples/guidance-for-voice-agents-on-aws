"""MonitorPublisher: writes short audio/levels segments for a watched call to S3, for
the dashboard API's polled `GET /api/calls/{call}/monitor` route to serve to the
browser, entirely off the call's hot path.

Gated entirely on `OUTBOUND_IVR_MONITOR_BUCKET`: when it's unset, `start()`/`publish()`/
`close()` are all immediate no-ops -- the same "safe to call even if never started"
shape `CallRecorder` already has. When it is set, this still only writes while someone
is watching this call: it polls the two zero-byte watch markers the dashboard API
writes (`<call_id>/watch/listen`, `<call_id>/watch/levels`) every `WATCH_POLL_SECONDS`
via `HeadObject`, and treats a marker as *fresh* (and therefore "being watched in that
mode") if its `LastModified` is within `WATCH_TTL_SECONDS` of now. `publish()` drops
every chunk while neither marker is fresh, and omits the chunk's PCM audio (keeping
only the RMS levels) unless the `listen` marker specifically is fresh -- a levels-only
viewer never causes audio to leave the Runtime.

`publish()` never blocks the caller: it buffers `SEGMENT_CHUNKS` chunks (~2s) and, once
full, enqueues the finished segment onto a bounded `asyncio.Queue` and returns
immediately. A worker task drains the queue and does the blocking `PutObject` via
`asyncio.to_thread`, the same shape `CallRecorder._writer` uses for its WAV writes. A
full queue drops the *oldest* queued segment rather than blocking or dropping the new
one -- a backlog here means a watcher has already fallen behind live audio, so the
newest segment is the one worth keeping. Any `HeadObject`/`PutObject` exception is
logged once per call (not once per poll tick/chunk -- a failing bucket would otherwise
flood the log) and dropped; nothing here is ever allowed to raise into the call path.

`close()` best-effort deletes every object this call ever wrote under `<call_id>/`,
markers included, bounded to a few seconds total so call teardown never hangs on it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import time
from typing import Any

import structlog

logger = structlog.get_logger("outbound_ivr.voice.monitor")

WATCH_POLL_SECONDS = 2.0
WATCH_TTL_SECONDS = 15
SEGMENT_CHUNKS = 10  # ~2s of 200ms chunks per segment.
CHUNK_MS = 200
QUEUE_MAXSIZE = 5
CLOSE_TIMEOUT_SECONDS = 5.0


class MonitorPublisher:
    """Writes mixed-call audio/levels segments to S3, only while someone is watching,
    never on the call's hot path. A no-op in every method when `OUTBOUND_IVR_MONITOR_BUCKET`
    is unset -- callers (`LiveKitTransport`, `CallRecorder`'s `on_frame` hook) always
    call into this; disablement lives here, not at call sites.
    """

    def __init__(self, call_id: str) -> None:
        self._call_id = call_id
        self._bucket = os.environ.get("OUTBOUND_IVR_MONITOR_BUCKET")
        self._enabled = bool(self._bucket)

        self._seg_seq = 0
        self._buffer: list[dict[str, Any]] = []
        self._buffer_t0: float | None = None
        self._watching = False
        self._listening = False

        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._poll_task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        # The worker's currently in-flight `PutObject`, if any -- tracked separately
        # from `_worker_task` so `close()` can wait for the real OS thread to finish
        # (see `_close_impl`) rather than merely cancelling the task wrapping it.
        self._inflight_put: asyncio.Future | None = None
        self._closing = False
        self._client: Any = None
        self._logged_poll_error = False
        self._logged_write_error = False

    @property
    def enabled(self) -> bool:
        """Whether this publisher can ever write anything -- `OUTBOUND_IVR_MONITOR_BUCKET` was
        set and `start()` didn't fail to stand up its S3 client. Callers (e.g.
        `LiveKitTransport`, deciding whether it needs a mixing path even when the call
        itself isn't being recorded) use this rather than reaching into private state.
        """
        return self._enabled

    def start(self) -> None:
        """Start the watch-marker poll and the write-worker task. No-op if disabled or
        already started.

        Never raises: this runs on the call-setup hot path, and the call must never
        wait on or fail because of the monitor. A `boto3.client()` construction failure
        (bad credentials, etc.) is logged once and leaves the publisher disabled, the
        same "log once, drop" contract the rest of this module already follows for
        `HeadObject`/`PutObject` failures.
        """
        if not self._enabled or self._poll_task is not None:
            return
        try:
            import boto3

            # AgentCore Runtime (and Lambda) set AWS_REGION to the deployment region;
            # the bucket this module talks to is always in that same region. Read it
            # here (not at import time) so a missing AWS_REGION is caught by the
            # "log once, disable" contract below instead of failing at import.
            self._client = boto3.client("s3", region_name=os.environ["AWS_REGION"])
            self._poll_task = asyncio.create_task(self._poll_watch())
            self._worker_task = asyncio.create_task(self._worker())
        except Exception:
            logger.exception("monitor_start_failed_disabling_monitor", call_id=self._call_id)
            self._enabled = False
            self._client = None

    def publish(self, *, far_rms: float, agent_rms: float, pcm: bytes) -> None:
        """Buffer one chunk for `call_id`. Non-blocking; drops the chunk immediately
        when disabled or nobody is currently watching. Once `SEGMENT_CHUNKS` chunks have
        accumulated, enqueues the finished segment for the worker task to write."""
        if not self._enabled or self._closing or not self._watching:
            return
        try:
            chunk: dict[str, Any] = {"far_rms": far_rms, "agent_rms": agent_rms}
            if self._listening:
                chunk["pcm"] = base64.b64encode(pcm).decode("ascii")

            if not self._buffer:
                self._buffer_t0 = time.time()
            self._buffer.append(chunk)

            if len(self._buffer) < SEGMENT_CHUNKS:
                return
            segment = {
                "seq": self._seg_seq,
                "t0": self._buffer_t0,
                "chunk_ms": CHUNK_MS,
                "chunks": self._buffer,
            }
            self._seg_seq += 1
            self._buffer = []
            self._buffer_t0 = None

            try:
                self._queue.put_nowait(segment)
            except asyncio.QueueFull:
                # Drop the oldest queued segment, not the new one -- see module
                # docstring.
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait(segment)
        except Exception:
            logger.exception("monitor_publish_buffer_chunk_failed", call_id=self._call_id)

    async def close(self) -> None:
        """Cancel the poll and worker tasks, drop any partial buffer, and best-effort
        delete every object this call wrote under `<call_id>/` (markers included).
        Idempotent, bounded to roughly `CLOSE_TIMEOUT_SECONDS`, and never raises --
        safe to call even if `start()` was never called."""
        try:
            await asyncio.wait_for(self._close_impl(), timeout=CLOSE_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("monitor_close_failed", call_id=self._call_id)

    async def _close_impl(self) -> None:
        # Stop new segments first: publish() drops everything from here on, and the
        # worker won't start another PutObject once it sees the flag.
        self._closing = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

        if self._worker_task is not None:
            # Cancelling a task while it's awaiting `asyncio.to_thread(...)` returns
            # control here immediately via CancelledError, but it does NOT stop the
            # underlying OS thread running the blocking `PutObject` -- that thread
            # keeps writing in the background. If the delete sweep below ran right
            # after, it could race an orphaned PutObject and leave a segment (possibly
            # containing raw audio) behind in S3. So: wait for any PutObject the
            # worker already started to actually finish -- a real wait on the
            # to_thread future, not a cancellation -- before cancelling the now-idle
            # worker task and running the sweep.
            # Drop queued segments so the worker has nothing left to start once the
            # in-flight write finishes; they would only be deleted again below.
            while not self._queue.empty():
                self._queue.get_nowait()
            pending_put = self._inflight_put
            if pending_put is not None:
                with contextlib.suppress(Exception):
                    await pending_put
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None

        self._buffer = []
        self._buffer_t0 = None

        if self._client is not None:
            try:
                await asyncio.to_thread(self._delete_all_objects)
            except Exception:
                logger.exception("monitor_cleanup_delete_failed", call_id=self._call_id)

    # ----------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------

    async def _poll_watch(self) -> None:
        try:
            while True:
                await self._refresh_watch()
                await asyncio.sleep(WATCH_POLL_SECONDS)
        except asyncio.CancelledError:
            return

    async def _refresh_watch(self) -> None:
        try:
            listening, watching = await asyncio.to_thread(self._check_markers)
        except Exception:
            if not self._logged_poll_error:
                logger.exception("monitor_watch_marker_poll_failed", call_id=self._call_id)
                self._logged_poll_error = True
            listening, watching = False, False

        self._listening = listening
        self._watching = watching
        if not watching:
            # Watching stopped mid-segment: the partial buffer can never be completed
            # (or shouldn't be published if it was only buffering for a levels-only
            # viewer that just left), so discard it rather than carry it forward.
            self._buffer = []
            self._buffer_t0 = None

    def _check_markers(self) -> tuple[bool, bool]:
        listen_fresh = self._marker_fresh("listen")
        levels_fresh = self._marker_fresh("levels")
        return listen_fresh, (listen_fresh or levels_fresh)

    def _marker_fresh(self, mode: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            response = self._client.head_object(
                Bucket=self._bucket, Key=f"{self._call_id}/watch/{mode}"
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "403", "NoSuchKey", "NotFound"):
                return False
            raise
        age = time.time() - response["LastModified"].timestamp()
        return age <= WATCH_TTL_SECONDS

    async def _worker(self) -> None:
        try:
            while True:
                segment = await self._queue.get()
                if self._closing:
                    return
                # Keep a handle to the in-flight PutObject outside this task so
                # `close()` can await its real completion -- see `_close_impl`.
                self._inflight_put = asyncio.ensure_future(
                    asyncio.to_thread(self._put_segment, segment)
                )
                try:
                    await self._inflight_put
                except Exception:
                    if not self._logged_write_error:
                        logger.exception("monitor_segment_write_failed", call_id=self._call_id)
                        self._logged_write_error = True
                finally:
                    self._inflight_put = None
        except asyncio.CancelledError:
            return

    def _put_segment(self, segment: dict[str, Any]) -> None:
        key = f"{self._call_id}/seg/{segment['seq']:08d}.json"
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(segment).encode("utf-8"),
            ContentType="application/json",
        )

    def _delete_all_objects(self) -> None:
        keys: list[dict[str, str]] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=f"{self._call_id}/"):
            keys.extend({"Key": obj["Key"]} for obj in page.get("Contents", []))

        for i in range(0, len(keys), 1000):  # DeleteObjects caps at 1000 keys per call.
            batch = keys[i : i + 1000]
            if batch:
                self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": batch})
