"""Lambda handlers behind API Gateway HTTP API for the deployed dashboard SPA.

Every case, patient and office record this handler reads or writes is synthetic demo
data. Adapting this handler to real patient data makes it PHI, which is the deployer's
own responsibility: a HIPAA-eligible service configuration, a Business Associate
Agreement with AWS, and your own compliance review, none of which this repository
provides.

See docs/design.md, "Dashboard (workstream 6)", and lambdas/api/README.md for routes,
IAM per route, env vars, and the `outbound-ivr-events` item shape.

One Lambda handles every route; `lambda_handler` dispatches on the HTTP API v2
`routeKey` (e.g. "GET /api/cases"). API Gateway's Cognito JWT authorizer rejects
unauthenticated requests before this function runs, so there is no auth check here.

Reuses `outbound_ivr.tools.store.DynamoDBCaseStore` for case/office reads (same decimal
handling and table-name env vars as the Gateway Lambda target in lambdas/tools/). The
events table, S3 case-record listing, and the AgentCore Runtime invoke aren't part of
that store's interface, so this talks to them directly with boto3.

`POST /api/calls` starts a call via `bedrock-agentcore:InvokeAgentRuntime` on the Outbound IVR
Runtime (`OUTBOUND_IVR_RUNTIME_ARN`, always set in the deployed stack): mints the call id
here and returns it. See lambdas/api/README.md, "Starting a call".
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import structlog

from outbound_ivr.tools.store import DynamoDBCaseStore


def _region() -> str:
    """Lambda always sets AWS_REGION to this function's own deployment region. Read
    fresh on every call, not captured once at import time, so a test (or a redeploy
    to a new region within the same warm container) that changes it afterward is
    actually honored."""
    return os.environ["AWS_REGION"]


def _decimals_to_native(obj: Any) -> Any:
    """boto3's DynamoDB resource returns numbers as Decimal; make them plain int/float."""
    if isinstance(obj, list):
        return [_decimals_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _decimals_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


_store: DynamoDBCaseStore | None = None


def _get_store() -> DynamoDBCaseStore:
    global _store
    if _store is None:
        _store = DynamoDBCaseStore(
            cases_table=os.environ.get("OUTBOUND_IVR_CASES_TABLE", "outbound-ivr-cases"),
            offices_table=os.environ.get("OUTBOUND_IVR_OFFICES_TABLE", "outbound-ivr-offices"),
            endpoint_url=os.environ.get("OUTBOUND_IVR_DYNAMODB_ENDPOINT"),
            region_name=_region(),
        )
    return _store


# boto3 clients, built once per container and reused across requests: building one
# (and its TLS connection) on every request was most of a warm request's time.
_clients: dict[str, Any] = {}


def _cached(name: str, build: Any) -> Any:
    if name not in _clients:
        _clients[name] = build()
    return _clients[name]


def _dynamodb_resource():
    import boto3

    return _cached(
        "dynamodb",
        lambda: boto3.resource(
            "dynamodb",
            endpoint_url=os.environ.get("OUTBOUND_IVR_DYNAMODB_ENDPOINT"),
            region_name=_region(),
        ),
    )


def _s3_client():
    import boto3

    return _cached("s3", lambda: boto3.client("s3", region_name=_region()))


def _agentcore_client():
    import boto3

    return _cached(
        "bedrock-agentcore", lambda: boto3.client("bedrock-agentcore", region_name=_region())
    )


def _response(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)


def _caller(event: dict) -> dict[str, str]:
    """Who made this request, from the Cognito JWT the authorizer already verified.

    API Gateway validated the token before this Lambda ran, so these claims are
    trustworthy without re-verifying anything here.
    """
    # `or {}` at every level, not `.get(k, {})`: an authorizer key that is present
    # with value None raises AttributeError on the next .get, and these call sites
    # all run after the real work succeeded, so that would turn a 200 into a 500
    # purely because a log line could not be written.
    context = event.get("requestContext") or {}
    claims = ((context.get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    return {"sub": claims.get("sub") or "unknown", "email": claims.get("email") or ""}


def _audit(event: dict, action: str, **fields: Any) -> None:
    """One structured line per read of call content.

    Recording URLs are presigned with this Lambda's own role, so S3 server access
    logs and CloudTrail data events can only ever say "the API Lambda read it".
    There was no other record, which meant that if a recording later turned out to
    contain something it should not have, nobody could say which operator had
    opened it. This is the smallest thing that answers that question; it is not a
    substitute for S3 access logging, which this stack still does not enable.
    """
    try:
        logger.info(
            "audit_event",
            action=action,
            **_caller(event),
            **fields,
        )
    except Exception:
        # An audit line is never worth failing the request that already succeeded.
        logger.exception("audit_line_write_failed", action=action)


# --------------------------------------------------------------------------
# GET /api/cases -- the case queue
# --------------------------------------------------------------------------


def handle_get_cases(event: dict) -> dict:
    """Every item in `outbound-ivr-cases`, newest-updated first."""
    table_name = os.environ.get("OUTBOUND_IVR_CASES_TABLE", "outbound-ivr-cases")
    table = _dynamodb_resource().Table(table_name)
    items: list[dict] = []
    resp = table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    items = [_decimals_to_native(i) for i in items]
    items.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    return _response(200, items)


# --------------------------------------------------------------------------
# GET /api/cases/{id} -- the case item plus its S3 case records
# --------------------------------------------------------------------------


def handle_get_case(event: dict) -> dict:
    case_id = event["pathParameters"]["id"]
    store = _get_store()
    case = store.get_case(case_id)
    if case is None:
        return _response(404, {"detail": "case not found"})

    bucket = os.environ.get("OUTBOUND_IVR_ARTIFACTS_BUCKET")
    records: list[dict] = []
    if bucket:
        s3 = _s3_client()
        # Convention this workstream defines for the agent/bridge to write under (see
        # lambdas/api/README.md, "S3 case records"): one JSON object per call attempt.
        prefix = f"cases/{case_id}/"
        paginator = s3.get_paginator("list_objects_v2")
        keys = [
            obj["Key"]
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
            for obj in page.get("Contents", [])
        ]
        # One record per call attempt; read them in parallel, not one after another.
        with ThreadPoolExecutor(max_workers=8) as pool:
            bodies = pool.map(
                lambda key: s3.get_object(Bucket=bucket, Key=key)["Body"].read(), keys
            )
            for body in bodies:
                try:
                    records.append(json.loads(body))
                except json.JSONDecodeError:
                    continue
        records.sort(key=lambda r: r.get("started_at") or "")

    # The Gateway/local-tools path (lambdas/tools/) writes confirmed answers straight to
    # `results` on the case item as the call progresses. Outbound IVR's in-process tools
    # (OUTBOUND_IVR_TOOLS=local) never touch the case item -- everything they collect lands
    # only in the S3 case record above -- so `results` (and `outcome`) stay empty on
    # that path even once the case is fully worked. Fall back to the latest record (by
    # `started_at`, i.e. the last one after the sort above) whenever the case item
    # itself doesn't carry them, so the dashboard sees the same evidence either way.
    latest = records[-1] if records else None
    if not case.get("results") and latest:
        case["results"] = latest.get("items", {})
    if not case.get("outcome") and latest and latest.get("outcome"):
        case["outcome"] = latest["outcome"]

    case["records"] = records
    # The case detail carries the transcript and the collected answers, so reading
    # it is a read of call content, same as fetching the recording.
    _audit(event, "case.read", case_id=case_id, records=len(records))
    return _response(200, case)


# --------------------------------------------------------------------------
# PATCH /api/cases/{id} {"fields": {...}} and/or {"reset": true} -- edit a case's
# info, or reset a case stuck in "queued" (or any other non-final state) back to a
# clean "pending" start.
#
# Editable fields (strings only, trimmed, max 200 chars; unknown fields -> 400):
# status (one of pending/queued/calling/complete/escalated), patient_name,
# patient_dob, member_id, auth_ref, procedure, payer, ordering_provider -- see
# `outbound_ivr.tools.store.default_case` for the item shape this allowlist is drawn from.
#
# reset=true: status -> "pending", attempts -> 0, and REMOVEs outcome, current_call,
# last_call, last_call_started_at. Always sets updated_at. If `fields` also sets
# "status", reset's own "pending" wins (reset is about getting unstuck, not about
# picking a status). Clearing attempts and last_call_started_at is also what lifts
# the attempt cap and the dial cooldown `_mark_case_queued` enforces, so reset is the
# documented way back from both.
#
# Refuses (409) to apply `fields` to a case whose current status is "calling",
# unless `reset` is also true -- editing a case mid-call out from under the agent
# would be worse than leaving it stuck. 404s if the case doesn't exist, both before
# the write (so a 404 doesn't silently also 409) and via a ConditionExpression on
# the write itself (attribute_exists(case_id)), in case the case was deleted between
# the read and the write.
# --------------------------------------------------------------------------

EDITABLE_STATUS_VALUES = {"pending", "queued", "calling", "complete", "escalated"}

# Every string field on `default_case` that makes sense for a human to correct by
# hand -- not case_id (the key), not missing_items/results/attempts/outcome/
# office_id/updated_at (structured, derived, or not meant for free-text editing).
EDITABLE_STRING_FIELDS = {
    "patient_name",
    "patient_dob",
    "member_id",
    "auth_ref",
    "procedure",
    "payer",
    "ordering_provider",
}

EDITABLE_FIELDS = EDITABLE_STRING_FIELDS | {"status"}


def _validate_edit_fields(fields: Any) -> dict[str, str]:
    """Returns the cleaned {field: value} to write, or raises TypeError/ValueError
    with a message safe to put straight into a 400 response body.
    """
    if not isinstance(fields, dict):
        raise TypeError("fields must be an object")
    cleaned: dict[str, str] = {}
    for key, value in fields.items():
        if key not in EDITABLE_FIELDS:
            raise ValueError(f"unknown field: {key!r}")
        if not isinstance(value, str):
            raise TypeError(f"{key} must be a string")
        value = value.strip()
        if len(value) > 200:
            raise ValueError(f"{key} must be at most 200 characters")
        if key == "status" and value not in EDITABLE_STATUS_VALUES:
            raise ValueError(f"status must be one of {sorted(EDITABLE_STATUS_VALUES)}")
        cleaned[key] = value
    return cleaned


def handle_patch_case(event: dict) -> dict:
    from botocore.exceptions import ClientError

    case_id = event["pathParameters"]["id"]
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"detail": "invalid JSON body"})
    if not isinstance(body, dict):
        return _response(400, {"detail": "invalid JSON body"})

    reset = bool(body.get("reset", False))
    raw_fields = body.get("fields") if body.get("fields") is not None else {}
    try:
        fields = _validate_edit_fields(raw_fields)
    except (TypeError, ValueError) as exc:
        return _response(400, {"detail": str(exc)})

    if not reset and not fields:
        return _response(400, {"detail": "nothing to update: provide fields and/or reset"})

    store = _get_store()
    case = store.get_case(case_id)
    if case is None:
        return _response(404, {"detail": "case not found"})
    if case.get("status") == "calling" and not reset:
        return _response(409, {"detail": "this case is calling; reset it before editing"})

    now = datetime.now(UTC).isoformat()
    set_clauses = ["updated_at = :now"]
    remove_clauses: list[str] = []
    names: dict[str, str] = {}
    values: dict[str, Any] = {":now": now}

    if reset:
        names["#status"] = "status"
        set_clauses.append("#status = :pending")
        set_clauses.append("attempts = :zero")
        values[":pending"] = "pending"
        values[":zero"] = 0
        # last_call_started_at goes too, so a reset also clears the dial cooldown
        # `_mark_case_queued` enforces. An operator who has deliberately reset a case
        # should not then be told to wait.
        remove_clauses.extend(["outcome", "current_call", "last_call", "last_call_started_at"])

    for key, value in fields.items():
        if reset and key == "status":
            # reset's own "pending" wins over an explicit status in the same request.
            continue
        # Every field name goes through ExpressionAttributeNames, not just
        # "status" -- "procedure" (one of EDITABLE_STRING_FIELDS) is itself a
        # DynamoDB reserved keyword, and there is no reason to special-case which
        # editable field names collide with the reserved list rather than
        # name-placeholdering all of them uniformly.
        names[f"#{key}"] = key
        set_clauses.append(f"#{key} = :f_{key}")
        values[f":f_{key}"] = value

    update_expression = "SET " + ", ".join(set_clauses)
    if remove_clauses:
        update_expression += " REMOVE " + ", ".join(remove_clauses)

    table_name = os.environ.get("OUTBOUND_IVR_CASES_TABLE", "outbound-ivr-cases")
    table = _dynamodb_resource().Table(table_name)
    kwargs: dict[str, Any] = {
        "Key": {"case_id": case_id},
        "UpdateExpression": update_expression,
        "ConditionExpression": "attribute_exists(case_id)",
        "ExpressionAttributeValues": values,
        "ReturnValues": "ALL_NEW",
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names

    try:
        resp = table.update_item(**kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _response(404, {"detail": "case not found"})
        raise

    return _response(200, _decimals_to_native(resp["Attributes"]))


# --------------------------------------------------------------------------
# GET /api/calls/{call}/events?after=<seq> -- live events, polled
# --------------------------------------------------------------------------


def handle_get_events(event: dict) -> dict:
    call = event["pathParameters"]["call"]
    query = event.get("queryStringParameters") or {}
    try:
        after = int(query.get("after", -1))
    except (TypeError, ValueError):
        after = -1

    from boto3.dynamodb.conditions import Key

    table_name = os.environ.get("OUTBOUND_IVR_EVENTS_TABLE", "outbound-ivr-events")
    table = _dynamodb_resource().Table(table_name)
    # Consistent reads, and every page: an event written a moment ago must not be
    # missed, and a long call can exceed one 1 MB page.
    kwargs = {
        "KeyConditionExpression": Key("call").eq(call) & Key("seq").gt(after),
        "ScanIndexForward": True,
        "ConsistentRead": True,
    }
    items: list[dict] = []
    while True:
        resp = table.query(**kwargs)
        items += [_decimals_to_native(i) for i in resp.get("Items", [])]
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return _response(200, items)


# --------------------------------------------------------------------------
# GET /api/recordings/{call} -- a presigned S3 URL, 5 minutes
# --------------------------------------------------------------------------


def handle_get_recording(event: dict) -> dict:
    from botocore.exceptions import ClientError

    call = event["pathParameters"]["call"]
    bucket = os.environ.get("OUTBOUND_IVR_ARTIFACTS_BUCKET")
    if not bucket:
        return _response(503, {"detail": "no artifacts bucket configured"})
    s3 = _s3_client()
    key = f"recordings/{call}.wav"
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey"):
            return _response(404, {"detail": "recording not found"})
        raise
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300
    )
    _audit(event, "recording.read", call=call, key=key)
    return _response(200, {"url": url})


# --------------------------------------------------------------------------
# GET /api/calls/{call}/monitor?mode=listen|levels&after=<seq> -- poll for Monitor
# segments the Runtime wrote to the dedicated monitor bucket (see
# outbound_ivr.voice.monitor.MonitorPublisher and infrastructure/stacks/monitor_stack.py), and
# (re)write this viewer's own watch marker so the Runtime knows whether anyone is
# watching, and whether to publish audio. Mirrors the polled
# GET /api/calls/{call}/events?after=<seq> route above; no WebSocket, no ticket, no
# LiveKit token -- everything the browser receives comes through this
# Cognito-protected API.
# --------------------------------------------------------------------------

MONITOR_WATCH_TTL_SECONDS = 15
MONITOR_WATCH_REFRESH_SECONDS = 5
MONITOR_MAX_SEGMENTS = 5
# A first poll skips a newest segment older than this: it is left over from an earlier
# viewer (segments stay until the call ends), not live audio.
MONITOR_LIVE_SECONDS = 10


def _is_canonical_call_id(call: str) -> bool:
    """A call id is a canonical lowercase uuid4 string -- the exact form `str(uuid4())`
    produces, and the only form `MonitorPublisher` and the Runtime write S3 keys
    under. Rejecting anything else (wrong case, braces, no hyphens, not a uuid at
    all) keeps every downstream key (`f"{call}/..."`) a literal, unambiguous S3
    prefix instead of something that merely parses as a uuid.
    """
    try:
        return str(UUID(call)) == call
    except (ValueError, AttributeError, TypeError):
        return False


def _touch_watch_marker(s3: Any, event: dict, bucket: str, call: str, mode: str) -> None:
    """Refresh the zero-byte `<call>/watch/<mode>` marker the Runtime polls for.

    Absent or older than `MONITOR_WATCH_TTL_SECONDS` (stale): this is a new or
    resumed viewer, so it's audited and the marker is (re)written. Between the
    refresh threshold and the TTL: a known, still-watching viewer is simply
    refreshed, no audit line for every poll. Fresher than that: skip the write
    entirely -- there is no need to re-PUT a marker on every 2-second poll.
    """
    from botocore.exceptions import ClientError

    key = f"{call}/watch/{mode}"
    age: float | None
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        age = (datetime.now(UTC) - head["LastModified"]).total_seconds()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in ("404", "403", "NoSuchKey"):
            raise
        age = None

    if age is None or age > MONITOR_WATCH_TTL_SECONDS:
        _audit(event, "monitor.watch", call=call, mode=mode)
        s3.put_object(Bucket=bucket, Key=key, Body=b"")
    elif age > MONITOR_WATCH_REFRESH_SECONDS:
        s3.put_object(Bucket=bucket, Key=key, Body=b"")


def handle_get_monitor(event: dict) -> dict:
    bucket = os.environ.get("OUTBOUND_IVR_MONITOR_BUCKET")
    if not bucket:
        return _response(404, {"error": "monitor not enabled"})

    call = event["pathParameters"]["call"]
    if not _is_canonical_call_id(call):
        return _response(400, {"error": "call must be a canonical uuid"})

    query = event.get("queryStringParameters") or {}
    mode = query.get("mode", "listen")
    if mode not in ("listen", "levels"):
        return _response(400, {"error": "mode must be 'listen' or 'levels'"})

    raw_after = query.get("after")
    try:
        after = int(raw_after) if raw_after is not None else -1
    except (TypeError, ValueError):
        return _response(400, {"error": "after must be an integer"})

    s3 = _s3_client()
    _touch_watch_marker(s3, event, bucket, call, mode)

    prefix = f"{call}/seg/"
    list_kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    if after >= 0:
        list_kwargs["StartAfter"] = f"{prefix}{after:08d}.json"
    paginator = s3.get_paginator("list_objects_v2")
    keys = [
        obj["Key"] for page in paginator.paginate(**list_kwargs) for obj in page.get("Contents", [])
    ]
    # ListObjectsV2 already returns keys in ascending order (including across
    # pages), and the zero-padded seq in each key makes that order numeric too.
    keys = keys[-1:] if after < 0 else keys[-MONITOR_MAX_SEGMENTS:]

    with ThreadPoolExecutor(max_workers=8) as pool:
        bodies = pool.map(lambda key: s3.get_object(Bucket=bucket, Key=key)["Body"].read(), keys)
        segments = [json.loads(body) for body in bodies]
    segments.sort(key=lambda segment: segment.get("seq", 0))

    after_out = segments[-1]["seq"] if segments else after
    if after < 0 and segments:
        age = datetime.now(UTC).timestamp() - float(segments[-1].get("t0", 0))
        if age > MONITOR_LIVE_SECONDS:
            # Not live: start the cursor past it so the viewer gets only new segments.
            segments = []

    if mode == "levels":
        # Defense in depth: the Runtime writes "pcm" only while this exact viewer is
        # listening, but a chunk in this segment could have been written while a
        # *different*, listening viewer was also watching this call.
        for segment in segments:
            for chunk in segment.get("chunks", []):
                chunk.pop("pcm", None)

    return _response(200, {"segments": segments, "after": after_out})


# --------------------------------------------------------------------------
# POST /api/calls {"case_id"} -- start a call, mark the case "queued"
#
# Invokes the Outbound IVR AgentCore Runtime directly (OUTBOUND_IVR_RUNTIME_ARN, always set in the
# deployed stack; see lambdas/api/README.md, "Starting a call"). This mints the call
# id here (there is no dispatcher to assign one later) and returns {"call": <call
# id>} -- the real call id, not an echo of case_id. A deploy that somehow left
# OUTBOUND_IVR_RUNTIME_ARN unset fails the request clearly (500) rather than queueing it
# somewhere nothing reads from.
# --------------------------------------------------------------------------


def _call_rate_limits() -> tuple[int, int]:
    """How many times one case may be dialed, and how long between dials.

    Read per request rather than at import so a redeploy-free env change takes
    effect on the next invocation (and so tests can set them).
    """
    return (
        int(os.environ.get("OUTBOUND_IVR_MAX_CALL_ATTEMPTS", "5")),
        int(os.environ.get("OUTBOUND_IVR_CALL_COOLDOWN_SECONDS", "60")),
    )


def _mark_case_queued(
    table, case_id: str, extra_values: dict | None = None, extra_set: str = ""
) -> None:
    """Flip a case's status to "queued", refusing a dial that is already in flight,
    over the attempt cap, or inside the cooldown.

    Three guards, deliberately in one ConditionExpression so they are evaluated by
    the same atomic write that claims the case. Checking them with a read first
    would reintroduce the race this is here to close:

    - **status.** "calling" was already refused; "queued" is now too. It was not,
      and that was the bug: two dashboard tabs, or one slow click plus a retry, both
      passed the guard while the first dial was still queued and Outbound IVR placed two
      simultaneous real phone calls for one case.
    - **attempts.** `attempts` is incremented by the Runtime when it records a call
      outcome (agentcore/livekit_app.py's `ADD attempts :one`), not here, so this
      counts calls that actually happened. A call that dies before writing an
      outcome therefore does not count against the cap; the cooldown is what bounds
      that case.
    - **cooldown.** `last_call_started_at` is written here, so it bounds dial rate
      even when nothing downstream completes. ISO-8601 UTC strings sort
      lexicographically, which is why a string comparison is a valid time
      comparison.

    Raises botocore.exceptions.ClientError (ConditionalCheckFailedException) when any
    guard refuses; callers turn that into a 409 whose detail says which one, via
    `_queue_refusal_detail`.
    """
    max_attempts, cooldown_seconds = _call_rate_limits()
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    cooldown_cutoff = (now_dt - timedelta(seconds=cooldown_seconds)).isoformat()
    values = {
        ":queued": "queued",
        ":calling": "calling",
        ":now": now,
        ":max_attempts": max_attempts,
        ":cooldown_cutoff": cooldown_cutoff,
        **(extra_values or {}),
    }
    table.update_item(
        Key={"case_id": case_id},
        UpdateExpression=(
            f"SET #status = :queued, updated_at = :now, last_call_started_at = :now{extra_set}"
        ),
        ConditionExpression=(
            "(attribute_not_exists(#status) OR (#status <> :calling AND #status <> :queued))"
            " AND (attribute_not_exists(attempts) OR attempts < :max_attempts)"
            " AND (attribute_not_exists(last_call_started_at)"
            " OR last_call_started_at < :cooldown_cutoff)"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=values,
    )


def _release_case_claim(table, case_id: str, call_id: str | None = None) -> None:
    """Undo `_mark_case_queued` when the dial never actually started.

    Without this, a transient failure from `InvokeAgentRuntime` or `SendMessage`
    left the case `queued` with no call behind it, and because the queued state is
    now itself a refusal, every later attempt 409'd forever until an operator
    reset the case by hand.

    Conditioned so it can only ever undo its own write: the status must still be
    `queued`, and on the AgentCore path `current_call` must still be the call id
    this request minted. A real call that started in the meantime is left alone.
    `last_call_started_at` goes too, since no call happened, so the cooldown
    should not count against the next attempt. Best effort by design: it must
    never replace the failure the caller is about to be told about.
    """
    from botocore.exceptions import ClientError

    values: dict[str, Any] = {
        ":pending": "pending",
        ":queued": "queued",
        ":now": datetime.now(UTC).isoformat(),
    }
    condition = "#status = :queued"
    if call_id is not None:
        values[":call"] = call_id
        condition += " AND current_call = :call"
    try:
        table.update_item(
            Key={"case_id": case_id},
            UpdateExpression=(
                "SET #status = :pending, updated_at = :now "
                "REMOVE current_call, last_call_started_at"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
        )
    except ClientError:
        logger.exception(
            "queued_claim_release_failed_manual_reset_needed",
            case_id=case_id,
        )


def _queue_refusal_detail(table, case_id: str) -> str:
    """Which of `_mark_case_queued`'s guards refused, as a sentence for the operator.

    DynamoDB reports a failed condition without saying which clause failed, so this
    re-reads the case. That read is only on the refusal path, after the atomic write
    has already been rejected, so a slightly stale answer can only produce a less
    precise message, never a second call.
    """
    max_attempts, cooldown_seconds = _call_rate_limits()
    item = table.get_item(Key={"case_id": case_id}).get("Item") or {}
    status = item.get("status")
    if status in ("calling", "queued"):
        return f"this case is already {status}"
    if int(item.get("attempts", 0) or 0) >= max_attempts:
        return f"this case has been called {max_attempts} times; reset it to dial again"
    return (
        f"this case was dialed in the last {cooldown_seconds} seconds; "
        "wait for the cooldown before dialing again"
    )


def _start_call_via_agentcore(case_id: str, runtime_arn: str) -> dict:
    """Mint call_id, record it on the case, invoke the Outbound IVR Runtime, return {"call": ...}.

    Per the shared start-call contract: write current_call=<call_id> on the case item
    *before* invoking the Runtime, then call bedrock-agentcore InvokeAgentRuntime with
    runtimeSessionId padded past AgentCore's 33-character minimum (call_id, a uuid4, is
    36 chars on its own, but the "-call" suffix is the contract's own padding, applied
    unconditionally) and payload {"action": "start_call", "call_id", "case_id"} --
    nothing about the office number or trunk: the Runtime reads those from its own env.
    """
    from botocore.exceptions import ClientError

    call_id = str(uuid4())
    table_name = os.environ.get("OUTBOUND_IVR_CASES_TABLE", "outbound-ivr-cases")
    table = _dynamodb_resource().Table(table_name)
    try:
        _mark_case_queued(
            table, case_id, extra_values={":call": call_id}, extra_set=", current_call = :call"
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _response(409, {"detail": _queue_refusal_detail(table, case_id)})
        raise

    runtime_session_id = f"{call_id}-call"
    payload = json.dumps({"action": "start_call", "call_id": call_id, "case_id": case_id}).encode(
        "utf-8"
    )
    try:
        _agentcore_client().invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=runtime_session_id,
            payload=payload,
            contentType="application/json",
        )
    except Exception:
        # The case is claimed but no call exists. Hand the claim back before
        # answering, or this case is stuck at 409 until someone resets it.
        logger.exception("invoke_agent_runtime_failed", case_id=case_id)
        _release_case_claim(table, case_id, call_id)
        return _response(502, {"detail": "could not start the call; the case was released"})
    return _response(202, {"call": call_id})


def handle_post_calls(event: dict) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"detail": "invalid JSON body"})
    case_id = body.get("case_id")
    if not case_id:
        return _response(400, {"detail": "case_id is required"})

    store = _get_store()
    case = store.get_case(case_id)
    if case is None:
        return _response(404, {"detail": "case not found"})

    runtime_arn = os.environ.get("OUTBOUND_IVR_RUNTIME_ARN")
    if not runtime_arn:
        logger.error("runtime_arn_not_set_cannot_start_call", case_id=case_id)
        return _response(500, {"detail": "OUTBOUND_IVR_RUNTIME_ARN is not configured"})
    return _start_call_via_agentcore(case_id, runtime_arn)


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------

ROUTES = {
    "GET /api/cases": handle_get_cases,
    "GET /api/cases/{id}": handle_get_case,
    "PATCH /api/cases/{id}": handle_patch_case,
    "GET /api/calls/{call}/events": handle_get_events,
    "GET /api/calls/{call}/monitor": handle_get_monitor,
    "GET /api/recordings/{call}": handle_get_recording,
    "POST /api/calls": handle_post_calls,
}


def lambda_handler(event: dict[str, Any], context: Any) -> dict:
    route_key = event.get("routeKey", "")
    handler = ROUTES.get(route_key)
    if handler is None:
        return _response(404, {"detail": f"no route for {route_key!r}"})
    try:
        return handler(event)
    except Exception:  # noqa: BLE001 - surface as a generic 500; details go to CloudWatch
        return _response(500, {"detail": "internal error"})
