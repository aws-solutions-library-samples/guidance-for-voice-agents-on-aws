"""lambdas/api/handler.py: the dashboard API Lambda, simulated API Gateway HTTP API v2
events with Cognito JWT authorizer claims (see lambdas/api/README.md for the contract).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from structlog.testing import capture_logs

from outbound_ivr.tools.store import DynamoDBCaseStore, default_case, default_office

LAMBDAS_API = Path(__file__).resolve().parents[2] / "src" / "lambdas" / "api"


def _load_handler_module():
    spec = importlib.util.spec_from_file_location(
        "outbound_ivr_lambda_api_handler", LAMBDAS_API / "handler.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handler = _load_handler_module()

CASES_TABLE = "outbound-ivr-cases"
OFFICES_TABLE = "outbound-ivr-offices"
EVENTS_TABLE = "outbound-ivr-events"
BUCKET = "outbound-ivr-artifacts-test"


def _api_event(
    route_key: str,
    *,
    path_params: dict | None = None,
    query: dict | None = None,
    body: dict | None = None,
) -> dict:
    """A simulated API Gateway v2 (HTTP API) event, with a Cognito JWT authorizer's
    claims attached the way the real integration would — unused by the handler (API
    Gateway itself enforces the authorizer), but present so the fixture matches reality.
    """
    return {
        "version": "2.0",
        "routeKey": route_key,
        "rawPath": route_key.split(" ", 1)[1].split("{")[0],
        "pathParameters": path_params or {},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "http": {"method": route_key.split(" ", 1)[0]},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "test-user",
                        "email": "sam@example.com",
                        "token_use": "access",
                    },
                    "scopes": [],
                }
            },
        },
    }


def _body(resp: dict):
    return json.loads(resp["body"])


@pytest.fixture(autouse=True)
def _reset_module_store(monkeypatch):
    monkeypatch.setattr(handler, "_store", None)
    monkeypatch.setattr(handler, "_clients", {})
    yield
    monkeypatch.setattr(handler, "_store", None)
    monkeypatch.setattr(handler, "_clients", {})


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", CASES_TABLE)
    monkeypatch.setenv("OUTBOUND_IVR_OFFICES_TABLE", OFFICES_TABLE)
    monkeypatch.setenv("OUTBOUND_IVR_EVENTS_TABLE", EVENTS_TABLE)
    monkeypatch.setenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", BUCKET)
    monkeypatch.delenv("OUTBOUND_IVR_DYNAMODB_ENDPOINT", raising=False)

    with mock_aws():
        DynamoDBCaseStore.ensure_tables(CASES_TABLE, OFFICES_TABLE, region_name="us-east-1")

        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
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

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        store = DynamoDBCaseStore(
            cases_table=CASES_TABLE, offices_table=OFFICES_TABLE, region_name="us-east-1"
        )
        store.put_office(
            default_office("office-1", name="AnyCompany Medical Group", number="+15555550100")
        )
        store.put_case(
            default_case(
                "case-1",
                patient_name="Jordan Ellis",
                pa_reference="PA-1",
                office_id="office-1",
                status="pending",
                missing_items=[{"key": "physical_therapy", "label": "PT", "question": "dates"}],
            )
        )

        yield {"dynamodb": dynamodb, "s3": s3, "store": store}


# --------------------------------------------------------------------------
# GET /api/cases
# --------------------------------------------------------------------------


def test_get_cases_returns_every_case(aws):
    resp = handler.handle_get_cases(_api_event("GET /api/cases"))
    assert resp["statusCode"] == 200
    cases = _body(resp)
    assert len(cases) == 1
    assert cases[0]["case_id"] == "case-1"
    assert cases[0]["status"] == "pending"


def test_get_cases_sorted_newest_updated_first(aws):
    aws["store"].put_case(default_case("case-2", updated_at="2099-01-01T00:00:00+00:00"))
    resp = handler.handle_get_cases(_api_event("GET /api/cases"))
    cases = _body(resp)
    assert cases[0]["case_id"] == "case-2"


# --------------------------------------------------------------------------
# GET /api/cases/{id}
# --------------------------------------------------------------------------


def test_get_case_not_found(aws):
    resp = handler.handle_get_case(_api_event("GET /api/cases/{id}", path_params={"id": "nope"}))
    assert resp["statusCode"] == 404


def test_get_case_includes_s3_records(aws):
    aws["s3"].put_object(
        Bucket=BUCKET,
        Key="cases/case-1/call-abc.json",
        Body=json.dumps({"started_at": "2026-01-01T00:00:00Z", "outcome": {"type": "complete"}}),
    )
    resp = handler.handle_get_case(_api_event("GET /api/cases/{id}", path_params={"id": "case-1"}))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["case_id"] == "case-1"
    assert len(body["records"]) == 1
    assert body["records"][0]["outcome"]["type"] == "complete"


def test_get_case_with_no_records(aws):
    resp = handler.handle_get_case(_api_event("GET /api/cases/{id}", path_params={"id": "case-1"}))
    assert _body(resp)["records"] == []


def test_get_case_falls_back_to_s3_record_when_results_empty(aws):
    """Outbound IVR's in-process tools (OUTBOUND_IVR_TOOLS=local) never write `results` on the case
    item -- only the S3 case record carries what got collected -- so `results` (and
    `outcome`) must come from the latest S3 record when the case item's own are empty.
    """
    aws["s3"].put_object(
        Bucket=BUCKET,
        Key="cases/case-1/call-abc.json",
        Body=json.dumps(
            {
                "started_at": "2026-01-01T00:00:00Z",
                "items": {
                    "physical_therapy": {
                        "value": "6 visits",
                        "confirmed": True,
                        "at": "2026-01-01T00:01:00Z",
                    }
                },
                "outcome": {"type": "complete", "summary": "done"},
            }
        ),
    )
    resp = handler.handle_get_case(_api_event("GET /api/cases/{id}", path_params={"id": "case-1"}))
    body = _body(resp)
    assert body["results"]["physical_therapy"]["confirmed"] is True
    assert body["outcome"]["type"] == "complete"


def test_get_case_prefers_existing_results_over_s3_fallback(aws):
    """The Gateway/local-tools path writes `results` directly -- that's the live,
    authoritative source while a call is in progress, so it must win over whatever a
    (possibly stale) S3 record from an earlier attempt says."""
    aws["dynamodb"].Table(CASES_TABLE).update_item(
        Key={"case_id": "case-1"},
        UpdateExpression="SET results = :r, outcome = :o",
        ExpressionAttributeValues={
            ":r": {
                "physical_therapy": {
                    "value": "4 visits",
                    "confirmed": True,
                    "at": "2026-01-02T00:00:00Z",
                }
            },
            ":o": {"type": "escalated", "reason": "no answer"},
        },
    )
    aws["s3"].put_object(
        Bucket=BUCKET,
        Key="cases/case-1/call-abc.json",
        Body=json.dumps(
            {
                "started_at": "2026-01-01T00:00:00Z",
                "items": {
                    "physical_therapy": {
                        "value": "stale",
                        "confirmed": True,
                        "at": "2026-01-01T00:01:00Z",
                    }
                },
                "outcome": {"type": "complete", "summary": "stale"},
            }
        ),
    )
    resp = handler.handle_get_case(_api_event("GET /api/cases/{id}", path_params={"id": "case-1"}))
    body = _body(resp)
    assert body["results"]["physical_therapy"]["value"] == "4 visits"
    assert body["outcome"]["type"] == "escalated"


# --------------------------------------------------------------------------
# PATCH /api/cases/{id}
# --------------------------------------------------------------------------


def _patch_case(case_id: str, body: dict) -> dict:
    return handler.handle_patch_case(
        _api_event("PATCH /api/cases/{id}", path_params={"id": case_id}, body=body)
    )


def test_patch_case_edits_allowed_fields(aws):
    resp = _patch_case(
        "case-1", {"fields": {"patient_name": "  Jordan A. Ellis  ", "procedure": "MRI"}}
    )
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["patient_name"] == "Jordan A. Ellis"
    assert body["procedure"] == "MRI"
    assert "updated_at" in body


def test_patch_case_edits_status(aws):
    resp = _patch_case("case-1", {"fields": {"status": "escalated"}})
    assert resp["statusCode"] == 200
    assert _body(resp)["status"] == "escalated"


def test_patch_case_rejects_unknown_field(aws):
    resp = _patch_case("case-1", {"fields": {"case_id": "nope"}})
    assert resp["statusCode"] == 400


def test_patch_case_rejects_non_string_value(aws):
    resp = _patch_case("case-1", {"fields": {"patient_name": 123}})
    assert resp["statusCode"] == 400


def test_patch_case_rejects_value_over_200_chars(aws):
    resp = _patch_case("case-1", {"fields": {"patient_name": "x" * 201}})
    assert resp["statusCode"] == 400


def test_patch_case_rejects_invalid_status(aws):
    resp = _patch_case("case-1", {"fields": {"status": "bogus"}})
    assert resp["statusCode"] == 400


def test_patch_case_requires_fields_or_reset(aws):
    resp = _patch_case("case-1", {})
    assert resp["statusCode"] == 400


def test_patch_case_not_found(aws):
    resp = _patch_case("nope", {"fields": {"patient_name": "X"}})
    assert resp["statusCode"] == 404


def test_patch_case_refuses_edit_while_calling(aws):
    aws["store"].set_status("case-1", "calling")
    resp = _patch_case("case-1", {"fields": {"patient_name": "X"}})
    assert resp["statusCode"] == 409


def test_patch_case_reset_allowed_while_calling(aws):
    aws["store"].set_status("case-1", "calling")
    resp = _patch_case("case-1", {"reset": True})
    assert resp["statusCode"] == 200
    assert _body(resp)["status"] == "pending"


def test_patch_case_reset_clears_outcome_and_calls(aws):
    table = aws["dynamodb"].Table(CASES_TABLE)
    table.update_item(
        Key={"case_id": "case-1"},
        UpdateExpression="SET #status = :calling, attempts = :three, outcome = :outcome, "
        "current_call = :cur, last_call = :last",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":calling": "calling",
            ":three": 3,
            ":outcome": {"type": "escalated"},
            ":cur": "call-abc",
            ":last": "call-xyz",
        },
    )
    resp = _patch_case("case-1", {"reset": True})
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert "outcome" not in body
    assert "current_call" not in body
    assert "last_call" not in body


def test_patch_case_reset_wins_over_fields_status(aws):
    resp = _patch_case("case-1", {"fields": {"status": "escalated"}, "reset": True})
    assert resp["statusCode"] == 200
    assert _body(resp)["status"] == "pending"


def test_patch_case_reset_combined_with_other_fields(aws):
    resp = _patch_case("case-1", {"fields": {"patient_name": "New Name"}, "reset": True})
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["status"] == "pending"
    assert body["patient_name"] == "New Name"


def test_patch_case_invalid_json_body(aws):
    event = _api_event("PATCH /api/cases/{id}", path_params={"id": "case-1"})
    event["body"] = "{not json"
    resp = handler.handle_patch_case(event)
    assert resp["statusCode"] == 400


# --------------------------------------------------------------------------
# GET /api/calls/{call}/events
# --------------------------------------------------------------------------


def _put_event(table, call: str, seq: int, **fields):
    item = {
        "call": call,
        "seq": seq,
        "t": "2026-01-01T00:00:00Z",
        "expires_at": 9999999999,
        **fields,
    }
    table.put_item(Item=item)


def test_get_events_returns_only_events_after_cursor(aws):
    table = aws["dynamodb"].Table(EVENTS_TABLE)
    _put_event(table, "call-1", 0, type="ready")
    _put_event(table, "call-1", 1, type="heard", text="hello")
    _put_event(table, "call-1", 2, type="said", text="hi")

    resp = handler.handle_get_events(
        _api_event(
            "GET /api/calls/{call}/events", path_params={"call": "call-1"}, query={"after": "0"}
        )
    )
    events = _body(resp)
    assert [e["seq"] for e in events] == [1, 2]
    assert events[0]["type"] == "heard"


def test_get_events_default_after_returns_everything(aws):
    table = aws["dynamodb"].Table(EVENTS_TABLE)
    _put_event(table, "call-1", 0, type="ready")
    resp = handler.handle_get_events(
        _api_event("GET /api/calls/{call}/events", path_params={"call": "call-1"})
    )
    assert len(_body(resp)) == 1


def test_get_events_only_for_the_requested_call(aws):
    table = aws["dynamodb"].Table(EVENTS_TABLE)
    _put_event(table, "call-1", 0, type="ready")
    _put_event(table, "call-2", 0, type="ready")
    resp = handler.handle_get_events(
        _api_event("GET /api/calls/{call}/events", path_params={"call": "call-2"})
    )
    events = _body(resp)
    assert len(events) == 1
    assert events[0]["call"] == "call-2"


# --------------------------------------------------------------------------
# GET /api/recordings/{call}
# --------------------------------------------------------------------------


def test_get_recording_not_found(aws):
    resp = handler.handle_get_recording(
        _api_event("GET /api/recordings/{call}", path_params={"call": "nope"})
    )
    assert resp["statusCode"] == 404


def test_get_recording_presigned_url(aws):
    aws["s3"].put_object(Bucket=BUCKET, Key="recordings/call-1.wav", Body=b"RIFF....")
    resp = handler.handle_get_recording(
        _api_event("GET /api/recordings/{call}", path_params={"call": "call-1"})
    )
    assert resp["statusCode"] == 200
    url = _body(resp)["url"]
    assert "call-1.wav" in url


def test_get_recording_no_bucket_configured(aws, monkeypatch):
    monkeypatch.delenv("OUTBOUND_IVR_ARTIFACTS_BUCKET", raising=False)
    resp = handler.handle_get_recording(
        _api_event("GET /api/recordings/{call}", path_params={"call": "call-1"})
    )
    assert resp["statusCode"] == 503


# --------------------------------------------------------------------------
# GET /api/calls/{call}/monitor?mode=listen|levels&after=<seq>
# --------------------------------------------------------------------------

MONITOR_BUCKET = "outbound-ivr-monitor-test"
MONITOR_CALL = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


@pytest.fixture
def monitor_bucket(monkeypatch):
    """The dedicated monitor bucket, plus the env var the route needs. Must run
    inside a `mock_aws()` context (the `aws` fixture already is).
    """
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=MONITOR_BUCKET)
    monkeypatch.setenv("OUTBOUND_IVR_MONITOR_BUCKET", MONITOR_BUCKET)
    return s3


def _monitor_event(
    *, mode: str | None = None, after: str | None = None, call: str = MONITOR_CALL
) -> dict:
    query: dict[str, str] = {}
    if mode is not None:
        query["mode"] = mode
    if after is not None:
        query["after"] = after
    return _api_event(
        "GET /api/calls/{call}/monitor",
        path_params={"call": call},
        query=query or None,
    )


def _put_segment(
    s3, seq: int, *, t0: float | None = None, with_pcm: bool = True, call: str = MONITOR_CALL
) -> None:
    if t0 is None:
        t0 = datetime.now(UTC).timestamp()  # live, unless a test says otherwise
    chunk: dict = {"far_rms": 0.1, "agent_rms": 0.0}
    if with_pcm:
        chunk["pcm"] = "AAA="
    body = {"seq": seq, "t0": t0, "chunk_ms": 200, "chunks": [chunk]}
    s3.put_object(
        Bucket=MONITOR_BUCKET,
        Key=f"{call}/seg/{seq:08d}.json",
        Body=json.dumps(body).encode("utf-8"),
        ContentType="application/json",
    )


def test_monitor_not_enabled_without_bucket(aws, monkeypatch):
    monkeypatch.delenv("OUTBOUND_IVR_MONITOR_BUCKET", raising=False)
    resp = handler.handle_get_monitor(_monitor_event())
    assert resp["statusCode"] == 404
    assert _body(resp) == {"error": "monitor not enabled"}


@pytest.mark.parametrize(
    "call",
    [
        "3FA85F64-5717-4562-B3FC-2C963F66AFA6",  # uppercase
        "not-a-uuid",
        "{3fa85f64-5717-4562-b3fc-2c963f66afa6}",  # braces
        "3fa85f6457174562b3fc2c963f66afa6",  # no hyphens
    ],
)
def test_monitor_rejects_non_canonical_call_id(aws, monitor_bucket, call):
    resp = handler.handle_get_monitor(_monitor_event(call=call))
    assert resp["statusCode"] == 400


def test_monitor_rejects_bad_mode(aws, monitor_bucket):
    resp = handler.handle_get_monitor(_monitor_event(mode="watch"))
    assert resp["statusCode"] == 400


def test_monitor_rejects_non_int_after(aws, monitor_bucket):
    resp = handler.handle_get_monitor(_monitor_event(after="soon"))
    assert resp["statusCode"] == 400


def test_monitor_writes_marker_and_audits_when_absent(aws, monitor_bucket):
    with capture_logs() as cap_logs:
        resp = handler.handle_get_monitor(_monitor_event())
    assert resp["statusCode"] == 200
    monitor_bucket.head_object(Bucket=MONITOR_BUCKET, Key=f"{MONITOR_CALL}/watch/listen")
    assert any(log.get("action") == "monitor.watch" for log in cap_logs)


def _age_marker(monkeypatch, seconds: float) -> None:
    """Make `_touch_watch_marker`'s `datetime.now(UTC)` appear `seconds` later than
    real wall-clock time, so a marker just written by the (un-mocked) moto S3 client
    reads back as `seconds` old -- moto stamps real `LastModified` timestamps on
    `PutObject`, and S3 has no API to set that timestamp directly.
    """
    import datetime as _dt

    class _AgedDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime.now(tz) + _dt.timedelta(seconds=seconds)

    monkeypatch.setattr(handler, "datetime", _AgedDatetime)


def test_monitor_writes_marker_and_audits_when_stale(aws, monitor_bucket, monkeypatch):
    monitor_bucket.put_object(Bucket=MONITOR_BUCKET, Key=f"{MONITOR_CALL}/watch/listen", Body=b"")
    _age_marker(monkeypatch, 20)  # older than MONITOR_WATCH_TTL_SECONDS (15)
    # Bracket the handler call itself, not the whole test run up to this point: moto
    # stamps the new marker's LastModified from the real system clock (unaffected by
    # _age_marker's monkeypatch of handler.datetime), so comparing against a fixed
    # wall-clock margin measured from before this call -- never from whatever else
    # ran earlier in the suite -- is what makes this deterministic rather than
    # flaky under a slow or loaded test run.
    before = datetime.now(UTC)
    with capture_logs() as cap_logs:
        resp = handler.handle_get_monitor(_monitor_event())
    assert resp["statusCode"] == 200
    head = monitor_bucket.head_object(Bucket=MONITOR_BUCKET, Key=f"{MONITOR_CALL}/watch/listen")
    # S3/moto's LastModified has second-level precision (truncated, not rounded), so
    # it can read up to a second before `before` even though the write happened
    # after it -- the 1-second slack is for that truncation, not for elapsed time.
    assert head["LastModified"] >= before.replace(microsecond=0) - timedelta(seconds=1)
    assert any(log.get("action") == "monitor.watch" for log in cap_logs)


def test_monitor_refreshes_marker_without_audit_when_aging(aws, monitor_bucket, monkeypatch):
    monitor_bucket.put_object(Bucket=MONITOR_BUCKET, Key=f"{MONITOR_CALL}/watch/listen", Body=b"")
    _age_marker(monkeypatch, 10)  # between MONITOR_WATCH_REFRESH_SECONDS (5) and the TTL (15)
    # See test_monitor_writes_marker_and_audits_when_stale above for why this is
    # bracketed around the call instead of using a margin from datetime.now().
    before = datetime.now(UTC)
    with capture_logs() as cap_logs:
        resp = handler.handle_get_monitor(_monitor_event())
    assert resp["statusCode"] == 200
    head = monitor_bucket.head_object(Bucket=MONITOR_BUCKET, Key=f"{MONITOR_CALL}/watch/listen")
    assert head["LastModified"] >= before.replace(microsecond=0) - timedelta(seconds=1)
    assert not any(log.get("action") == "monitor.watch" for log in cap_logs)


def test_monitor_skips_marker_write_when_fresh(aws, monitor_bucket, monkeypatch):
    monitor_bucket.put_object(Bucket=MONITOR_BUCKET, Key=f"{MONITOR_CALL}/watch/listen", Body=b"")
    original_last_modified = monitor_bucket.head_object(
        Bucket=MONITOR_BUCKET, Key=f"{MONITOR_CALL}/watch/listen"
    )["LastModified"]
    _age_marker(monkeypatch, 2)  # younger than MONITOR_WATCH_REFRESH_SECONDS (5)

    with capture_logs() as cap_logs:
        resp = handler.handle_get_monitor(_monitor_event())
    assert resp["statusCode"] == 200
    head = monitor_bucket.head_object(Bucket=MONITOR_BUCKET, Key=f"{MONITOR_CALL}/watch/listen")
    assert head["LastModified"] == original_last_modified
    assert not any(log.get("action") == "monitor.watch" for log in cap_logs)


def test_monitor_after_negative_returns_only_newest_segment(aws, monitor_bucket):
    now = datetime.now(UTC).timestamp()
    for seq in range(3):
        _put_segment(monitor_bucket, seq, t0=now - 2 * (2 - seq))
    resp = handler.handle_get_monitor(_monitor_event(after="-1"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert [s["seq"] for s in body["segments"]] == [2]
    assert body["after"] == 2


def test_monitor_first_poll_skips_a_stale_newest_segment(aws, monitor_bucket):
    """Segments from an earlier viewer stay until the call ends; a new viewer's first
    poll must not play them, only move its cursor past them."""
    old = datetime.now(UTC).timestamp() - 300
    for seq in range(3):
        _put_segment(monitor_bucket, seq, t0=old + seq)
    body = _body(handler.handle_get_monitor(_monitor_event(after="-1")))
    assert body["segments"] == []
    assert body["after"] == 2


def test_monitor_after_n_uses_start_after_and_caps_at_five_ascending(aws, monitor_bucket):
    for seq in range(8):
        _put_segment(monitor_bucket, seq)
    resp = handler.handle_get_monitor(_monitor_event(after="0"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert [s["seq"] for s in body["segments"]] == [3, 4, 5, 6, 7]
    assert body["after"] == 7


def test_monitor_paginates_list_objects(aws, monitor_bucket, monkeypatch):
    """`ListObjectsV2` pagination is followed to the end before deciding which
    segments to keep, not just the first page -- forced here by swapping in a fake
    paginator (on the exact client instance `handle_get_monitor` uses) that yields
    one key per page instead of moto's usual one-page-for-4-objects response.
    """
    for seq in range(4):
        _put_segment(monitor_bucket, seq)

    # The real client `handle_get_monitor` will use (cached by `_s3_client`), not
    # the `monitor_bucket` fixture's own client -- they are separate boto3 client
    # instances even though moto backs both with the same in-memory S3 state.
    s3 = handler._s3_client()
    real_paginator = s3.get_paginator("list_objects_v2")

    class _OnePerPagePaginator:
        def paginate(self, *args, **kwargs):
            for page in real_paginator.paginate(*args, **kwargs):
                for obj in page.get("Contents", []):
                    yield {"Contents": [obj]}

    monkeypatch.setattr(s3, "get_paginator", lambda name: _OnePerPagePaginator())
    resp = handler.handle_get_monitor(_monitor_event(after="-1"))
    assert resp["statusCode"] == 200
    assert _body(resp)["segments"][0]["seq"] == 3


def test_monitor_levels_mode_strips_pcm_from_every_chunk(aws, monitor_bucket):
    s3 = monitor_bucket
    body = {
        "seq": 0,
        "t0": datetime.now(UTC).timestamp(),
        "chunk_ms": 200,
        "chunks": [
            {"far_rms": 0.1, "agent_rms": 0.0, "pcm": "AAA="},
            {"far_rms": 0.2, "agent_rms": 0.1, "pcm": "BBB="},
        ],
    }
    s3.put_object(
        Bucket=MONITOR_BUCKET,
        Key=f"{MONITOR_CALL}/seg/00000000.json",
        Body=json.dumps(body).encode("utf-8"),
    )
    resp = handler.handle_get_monitor(_monitor_event(mode="levels", after="-1"))
    assert resp["statusCode"] == 200
    chunks = _body(resp)["segments"][0]["chunks"]
    assert len(chunks) == 2
    assert all("pcm" not in chunk for chunk in chunks)
    assert all(set(chunk) == {"far_rms", "agent_rms"} for chunk in chunks)


def test_monitor_empty_result_echoes_after(aws, monitor_bucket):
    resp = handler.handle_get_monitor(_monitor_event(after="7"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["segments"] == []
    assert body["after"] == 7


def test_monitor_empty_result_echoes_default_after(aws, monitor_bucket):
    resp = handler.handle_get_monitor(_monitor_event())
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["segments"] == []
    assert body["after"] == -1


# --------------------------------------------------------------------------
# POST /api/calls, OUTBOUND_IVR_RUNTIME_ARN set (always true in the deployed stack)
# --------------------------------------------------------------------------

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/outbound-ivr-runtime"


class _StubAgentCoreClient:
    """A stand-in for boto3's bedrock-agentcore client -- there is no moto backend for
    this service yet, so the AgentCore invoke is stubbed rather than mocked via AWS.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {"contentType": "application/json", "response": b""}


@pytest.fixture
def agentcore(aws, monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_RUNTIME_ARN", RUNTIME_ARN)
    stub = _StubAgentCoreClient()
    monkeypatch.setattr(handler, "_agentcore_client", lambda: stub)
    return stub


def test_post_calls_via_agentcore_mints_call_and_invokes_runtime(aws, agentcore):
    resp = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert resp["statusCode"] == 202
    body = _body(resp)
    call_id = body["call"]
    assert "case_id" not in body  # the real call id, not an echo of case_id

    import uuid

    uuid.UUID(call_id)  # a real uuid4, not just any string

    case = aws["store"].get_case("case-1")
    assert case["status"] == "queued"
    assert case["current_call"] == call_id

    assert len(agentcore.calls) == 1
    kwargs = agentcore.calls[0]
    assert kwargs["agentRuntimeArn"] == RUNTIME_ARN
    assert kwargs["runtimeSessionId"] == f"{call_id}-call"
    assert len(kwargs["runtimeSessionId"]) >= 33  # AgentCore's own minimum
    assert kwargs["contentType"] == "application/json"
    payload = json.loads(kwargs["payload"])
    assert payload == {"action": "start_call", "call_id": call_id, "case_id": "case-1"}


def test_post_calls_via_agentcore_refuses_when_already_calling(aws, agentcore):
    table = aws["dynamodb"].Table(CASES_TABLE)
    table.update_item(
        Key={"case_id": "case-1"},
        UpdateExpression="SET #status = :calling",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":calling": "calling"},
    )
    resp = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert resp["statusCode"] == 409
    assert agentcore.calls == []


def test_post_calls_via_agentcore_does_not_require_office_number(aws, agentcore):
    aws["store"].put_office(default_office("office-2", name="No Number Clinic"))
    aws["store"].put_case(default_case("case-2", office_id="office-2"))
    resp = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-2"}))
    assert resp["statusCode"] == 202
    assert len(agentcore.calls) == 1


def test_post_calls_unknown_case_before_invoking_the_runtime(aws, agentcore):
    resp = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "nope"}))
    assert resp["statusCode"] == 404
    assert agentcore.calls == []


def test_post_calls_missing_case_id(aws, agentcore):
    resp = handler.handle_post_calls(_api_event("POST /api/calls", body={}))
    assert resp["statusCode"] == 400


def test_post_calls_refuses_a_second_dial_while_the_first_is_still_queued(aws, agentcore):
    """The double-dial race. Two tabs, or a click plus a retry, used to both pass the
    guard while the first dial sat in "queued" and place two real phone calls for one
    case. The second attempt must be refused by the same atomic write that claims it.
    """
    first = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert first["statusCode"] == 202
    assert aws["store"].get_case("case-1")["status"] == "queued"

    second = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert second["statusCode"] == 409
    assert "already queued" in json.loads(second["body"])["detail"]

    # One call started downstream, not two.
    assert len(agentcore.calls) == 1


def test_post_calls_refuses_inside_the_cooldown(aws, agentcore, monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_CALL_COOLDOWN_SECONDS", "3600")
    handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    # Back to a dialable status, so only the cooldown can refuse the next attempt.
    aws["dynamodb"].Table(CASES_TABLE).update_item(
        Key={"case_id": "case-1"},
        UpdateExpression="SET #status = :pending",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":pending": "pending"},
    )

    resp = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert resp["statusCode"] == 409
    assert "cooldown" in json.loads(resp["body"])["detail"]


def test_post_calls_refuses_past_the_attempt_cap(aws, agentcore, monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_MAX_CALL_ATTEMPTS", "2")
    monkeypatch.setenv("OUTBOUND_IVR_CALL_COOLDOWN_SECONDS", "0")
    aws["dynamodb"].Table(CASES_TABLE).update_item(
        Key={"case_id": "case-1"},
        UpdateExpression="SET attempts = :two",
        ExpressionAttributeValues={":two": 2},
    )

    resp = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert resp["statusCode"] == 409
    assert "reset it to dial again" in json.loads(resp["body"])["detail"]


def test_reset_clears_the_cooldown_and_the_attempt_cap(aws, agentcore, monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_MAX_CALL_ATTEMPTS", "1")
    monkeypatch.setenv("OUTBOUND_IVR_CALL_COOLDOWN_SECONDS", "3600")
    first = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert first["statusCode"] == 202

    reset = handler.handle_patch_case(
        _api_event("PATCH /api/cases/{id}", path_params={"id": "case-1"}, body={"reset": True})
    )
    assert reset["statusCode"] == 200
    body = json.loads(reset["body"])
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert "last_call_started_at" not in body

    again = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert again["statusCode"] == 202, "reset must be the documented way back from both guards"


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------


def test_lambda_handler_dispatches_by_route_key(aws):
    resp = handler.lambda_handler(_api_event("GET /api/cases"), None)
    assert resp["statusCode"] == 200


def test_lambda_handler_unknown_route(aws):
    resp = handler.lambda_handler(_api_event("DELETE /api/cases"), None)
    assert resp["statusCode"] == 404


def test_lambda_handler_catches_unexpected_errors(aws, monkeypatch):
    def _boom(_event):
        raise RuntimeError("boom")

    monkeypatch.setitem(handler.ROUTES, "GET /api/cases", _boom)
    resp = handler.lambda_handler(_api_event("GET /api/cases"), None)
    assert resp["statusCode"] == 500


def test_aws_clients_are_built_once_per_container(monkeypatch):
    """A new boto3 client (and TLS connection) per request made warm requests ~300 ms."""
    built = []

    def fake_client(name, **kwargs):
        built.append(name)
        return object()

    monkeypatch.setattr("boto3.client", fake_client)
    first = handler._s3_client()
    assert handler._s3_client() is first
    assert built == ["s3"]


# --------------------------------------------------------------------------
# Who read what. Presigned recording URLs are signed with the Lambda's own
# role, so S3 access logs and CloudTrail can only ever say "the API Lambda
# read it" -- there was no record of which operator opened a recording.
# --------------------------------------------------------------------------


def _audit_lines(cap_logs) -> list[dict]:
    return [log for log in cap_logs if log.get("event") == "audit_event"]


def test_reading_a_recording_records_who_asked(aws):
    aws["s3"].put_object(Bucket=BUCKET, Key="recordings/call-1.wav", Body=b"RIFF")
    with capture_logs() as cap_logs:
        resp = handler.handle_get_recording(
            _api_event("GET /api/recordings/{call}", path_params={"call": "call-1"})
        )
    assert resp["statusCode"] == 200

    (entry,) = [e for e in _audit_lines(cap_logs) if e["action"] == "recording.read"]
    assert entry["call"] == "call-1"
    assert entry["sub"] == "test-user"
    assert entry["email"] == "sam@example.com"


def test_a_missing_recording_is_not_audited_as_a_read(aws):
    with capture_logs() as cap_logs:
        resp = handler.handle_get_recording(
            _api_event("GET /api/recordings/{call}", path_params={"call": "nope"})
        )
    assert resp["statusCode"] == 404
    assert not [e for e in _audit_lines(cap_logs) if e["action"] == "recording.read"]


def test_reading_a_case_detail_records_who_asked(aws):
    with capture_logs() as cap_logs:
        resp = handler.handle_get_case(
            _api_event("GET /api/cases/{id}", path_params={"id": "case-1"})
        )
    assert resp["statusCode"] == 200
    (entry,) = [e for e in _audit_lines(cap_logs) if e["action"] == "case.read"]
    assert entry["case_id"] == "case-1"
    assert entry["sub"] == "test-user"


def test_an_unauthenticated_shaped_event_still_logs_something_usable(aws):
    """API Gateway enforces the authorizer, so this should not happen -- but an
    audit line that throws on a missing claim would take the route down with it."""
    event = _api_event("GET /api/cases/{id}", path_params={"id": "case-1"})
    event["requestContext"] = {}
    with capture_logs() as cap_logs:
        resp = handler.handle_get_case(event)
    assert resp["statusCode"] == 200
    (entry,) = [e for e in _audit_lines(cap_logs) if e["action"] == "case.read"]
    assert entry["sub"] == "unknown"


# --------------------------------------------------------------------------
# A dial that never starts has to hand the case back. The claim is atomic and
# the queued state is itself a refusal now, so a case left queued with no call
# behind it 409s forever until an operator resets it by hand.
# --------------------------------------------------------------------------


class _RaisingAgentCoreClient:
    def invoke_agent_runtime(self, **kwargs):
        raise RuntimeError("ThrottlingException: rate exceeded")


def test_a_failed_runtime_invoke_releases_the_case(aws, monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_RUNTIME_ARN", RUNTIME_ARN)
    monkeypatch.setattr(handler, "_clients", {})
    monkeypatch.setattr(handler, "_agentcore_client", lambda: _RaisingAgentCoreClient())

    resp = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert resp["statusCode"] == 502

    case = aws["store"].get_case("case-1")
    assert case["status"] == "pending", "a case with no call behind it must not stay queued"
    assert "current_call" not in case
    assert "last_call_started_at" not in case, "no call happened, so no cooldown"


def test_release_leaves_a_case_the_runtime_already_marked_calling(aws):
    """An invoke that errored on the way back (a read timeout) can still have started
    the call. Once the Runtime has dialed and marked the case "calling", the release
    must not hand that live call's case back as "pending"."""
    table = aws["dynamodb"].Table(CASES_TABLE)
    table.update_item(
        Key={"case_id": "case-1"},
        UpdateExpression="SET #status = :calling, current_call = :call",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":calling": "calling", ":call": "call-live"},
    )

    handler._release_case_claim(table, "case-1", "call-live")

    case = aws["store"].get_case("case-1")
    assert case["status"] == "calling"
    assert case["current_call"] == "call-live"


def test_a_released_case_can_be_dialed_again(aws, agentcore, monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_RUNTIME_ARN", RUNTIME_ARN)
    monkeypatch.setattr(handler, "_agentcore_client", lambda: _RaisingAgentCoreClient())
    first = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert first["statusCode"] == 502

    monkeypatch.setattr(handler, "_agentcore_client", lambda: agentcore)
    second = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert second["statusCode"] == 202, "the release must leave the case dialable"


def test_the_agentcore_client_never_retries_invoke_agent_runtime(monkeypatch):
    """`invoke_agent_runtime` places a real outbound phone call, so it is not
    idempotent: boto3's default retries could re-send it after a read timeout and start
    a second call for the same case. The client is built with a single attempt; a failed
    invoke then goes straight to the release path above.

    Sends one request that fails with a retryable 503 and counts how often it went out:
    botocore's own `max_attempts: 1` would still send it twice."""
    from botocore.awsrequest import AWSResponse
    from botocore.exceptions import ClientError

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(handler, "_clients", {})
    client = handler._agentcore_client()
    sent: list[str] = []

    class _Raw:
        def stream(self, **kwargs):
            yield b""

    def _answer_503(request, **kwargs):
        sent.append(request.url)
        return AWSResponse(request.url, 503, {}, _Raw())

    client.meta.events.register("before-send.bedrock-agentcore.InvokeAgentRuntime", _answer_503)

    with pytest.raises(ClientError):
        client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN, runtimeSessionId="x" * 33, payload=b"{}"
        )

    assert client.meta.config.retries["total_max_attempts"] == 1
    assert len(sent) == 1, "invoke_agent_runtime must be sent exactly once, never retried"


def test_missing_runtime_arn_does_not_claim_the_case(aws, monkeypatch):
    """A deploy that somehow left OUTBOUND_IVR_RUNTIME_ARN unset must fail clearly (500)
    rather than claiming a case it cannot start a call for."""
    monkeypatch.delenv("OUTBOUND_IVR_RUNTIME_ARN", raising=False)
    resp = handler.handle_post_calls(_api_event("POST /api/calls", body={"case_id": "case-1"}))
    assert resp["statusCode"] == 500
    assert aws["store"].get_case("case-1")["status"] == "pending"


def test_an_audit_line_cannot_turn_a_successful_read_into_a_500(aws):
    """Every audit call site runs after the real work succeeded. An authorizer
    key present with value None used to raise AttributeError inside _caller."""
    event = _api_event("GET /api/cases/{id}", path_params={"id": "case-1"})
    event["requestContext"]["authorizer"] = None
    resp = handler.handle_get_case(event)
    assert resp["statusCode"] == 200
