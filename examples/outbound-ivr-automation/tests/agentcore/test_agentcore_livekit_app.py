"""`agentcore/livekit_app.py`: the AgentCore entrypoint that starts an outbound LiveKit
call in the background and returns at once. Against fakes of `outbound_ivr.voice.livekit_calls`
and `outbound_ivr.voice.agent.handle_call` -- no real LiveKit server, no real AgentCore
runtime, no AWS.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("bedrock_agentcore")
pytest.importorskip("strands")

from agentcore import livekit_app

CALL_ID = "call-123"
CASE_ID = "CASE-1"


# --------------------------------------------------------------------------
# start_call: validates the payload, starts the background task, returns at once.
# --------------------------------------------------------------------------


async def test_start_call_rejects_an_unknown_action():
    result = await livekit_app.start_call(
        {"action": "dial", "call_id": CALL_ID, "case_id": CASE_ID}
    )

    assert "error" in result
    assert "dial" in result["error"]


async def test_start_call_rejects_a_missing_action():
    result = await livekit_app.start_call({"call_id": CALL_ID, "case_id": CASE_ID})

    assert "error" in result


@pytest.mark.parametrize("missing", ["call_id", "case_id"])
async def test_start_call_requires_both_ids(missing):
    payload = {"action": "start_call", "call_id": CALL_ID, "case_id": CASE_ID}
    payload.pop(missing)

    result = await livekit_app.start_call(payload)

    assert "error" in result
    assert "required" in result["error"]


async def test_start_call_returns_at_once_and_schedules_the_call_in_the_background(monkeypatch):
    started = asyncio.Event()
    seen: list[tuple[str, str]] = []

    async def fake_run_call_tracked(call_id: str, case_id: str) -> None:
        seen.append((call_id, case_id))
        started.set()

    monkeypatch.setattr(livekit_app, "_run_call_tracked", fake_run_call_tracked)

    result = await livekit_app.start_call(
        {"action": "start_call", "call_id": CALL_ID, "case_id": CASE_ID}
    )

    assert result == {"call": CALL_ID, "status": "started"}
    # The background task hasn't necessarily run a single step yet -- start_call must
    # not have awaited it (that's the whole point of "returns at once").
    await asyncio.wait_for(started.wait(), timeout=1)
    assert seen == [(CALL_ID, CASE_ID)]


# --------------------------------------------------------------------------
# _run_call_tracked: HealthyBusy bookkeeping around _run_call.
# --------------------------------------------------------------------------


async def test_run_call_tracked_marks_an_async_task_busy_then_completes_it(monkeypatch):
    add_calls: list[tuple[str, dict]] = []
    complete_calls: list[int] = []

    def fake_add(name, metadata=None):
        add_calls.append((name, metadata))
        return 42

    def fake_complete(task_id):
        complete_calls.append(task_id)
        return True

    monkeypatch.setattr(livekit_app.app, "add_async_task", fake_add)
    monkeypatch.setattr(livekit_app.app, "complete_async_task", fake_complete)

    ran = []

    async def fake_run_call(call_id, case_id):
        ran.append((call_id, case_id))

    monkeypatch.setattr(livekit_app, "_run_call", fake_run_call)

    await livekit_app._run_call_tracked(CALL_ID, CASE_ID)

    assert ran == [(CALL_ID, CASE_ID)]
    assert add_calls == [("call", {"call_id": CALL_ID})]
    assert complete_calls == [42]


async def test_run_call_tracked_completes_the_task_even_if_run_call_raises(monkeypatch):
    complete_calls: list[int] = []
    monkeypatch.setattr(livekit_app.app, "add_async_task", lambda name, metadata=None: 7)
    monkeypatch.setattr(livekit_app.app, "complete_async_task", complete_calls.append)

    async def fake_run_call(call_id, case_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(livekit_app, "_run_call", fake_run_call)

    await livekit_app._run_call_tracked(CALL_ID, CASE_ID)  # must not raise

    assert complete_calls == [7]


# --------------------------------------------------------------------------
# _TaskAsProcess: the .returncode adapter wait_for_agent_join polls.
# --------------------------------------------------------------------------


async def test_task_as_process_returncode_is_none_while_running():
    task = asyncio.create_task(asyncio.sleep(10))
    adapter = livekit_app._TaskAsProcess(task)

    assert adapter.returncode is None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_task_as_process_returncode_is_zero_once_done_cleanly():
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    adapter = livekit_app._TaskAsProcess(task)

    assert adapter.returncode == 0


async def test_task_as_process_returncode_is_nonzero_once_failed():
    async def _boom():
        raise RuntimeError("boom")

    task = asyncio.create_task(_boom())
    with pytest.raises(RuntimeError):
        await task
    adapter = livekit_app._TaskAsProcess(task)

    assert adapter.returncode == 1


# --------------------------------------------------------------------------
# _run_call: room -> agent (in-process) -> dial -> cleanup, against fakes.
# --------------------------------------------------------------------------


class _FakeLkapi:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _wire_run_call(monkeypatch, *, handle_call=None):
    """Monkeypatch every `livekit_calls` call `_run_call` makes, and `agent.handle_call`
    itself, recording the order and arguments each was called with."""
    calls: list[tuple[str, tuple, dict]] = []
    lkapi = _FakeLkapi()

    def fake_make_admin_client():
        # Not async: the real outbound_ivr.voice.livekit_calls.make_admin_client() is a plain
        # sync function (it only builds a client object; _run_call doesn't await it).
        calls.append(("make_admin_client", (), {}))
        return lkapi

    async def fake_create_room(client, call_id, **kw):
        calls.append(("create_room", (call_id,), kw))

    async def fake_wait_for_agent_join(client, call_id, proc, identity, timeout):
        # The real wait_for_agent_join polls the room over several event-loop turns,
        # which is what gives the just-created `call_task` (agent.handle_call) its
        # first chance to actually run before dial_office is called; one bare `await`
        # here does the same for these tests' fakes.
        await asyncio.sleep(0)
        calls.append(("wait_for_agent_join", (call_id, identity, timeout), {}))

    async def fake_dial_office(client, call_id, sip_identity, **kw):
        calls.append(("dial_office", (call_id, sip_identity), kw))

    async def fake_delete_room(client, call_id):
        calls.append(("delete_room", (call_id,), {}))

    monkeypatch.setattr(livekit_app.livekit_calls, "make_admin_client", fake_make_admin_client)
    monkeypatch.setattr(livekit_app.livekit_calls, "resolve_api_secret", lambda: "resolved-secret")
    monkeypatch.setattr(livekit_app.livekit_calls, "create_room", fake_create_room)
    monkeypatch.setattr(livekit_app.livekit_calls, "wait_for_agent_join", fake_wait_for_agent_join)
    monkeypatch.setattr(livekit_app.livekit_calls, "dial_office", fake_dial_office)
    monkeypatch.setattr(livekit_app.livekit_calls, "delete_room", fake_delete_room)

    async def default_handle_call(transport):
        calls.append(("handle_call", (transport.call_uuid or transport._room_name,), {}))

    monkeypatch.setattr(livekit_app.agent, "handle_call", handle_call or default_handle_call)
    return calls, lkapi


async def test_run_call_sequences_room_then_agent_then_dial_then_cleanup(monkeypatch):
    calls, lkapi = _wire_run_call(monkeypatch)
    monkeypatch.setenv("OUTBOUND_IVR_OFFICE_NUMBER", "+15555550101")
    monkeypatch.setenv("OUTBOUND_IVR_TRUNK_NAME", "outbound-ivr-chime-outbound")

    await livekit_app._run_call(CALL_ID, CASE_ID)

    names = [c[0] for c in calls]
    index = {name: names.index(name) for name in names}
    # Room created, then the agent is given a chance to join (in-process, no
    # subprocess) before the office is ever dialed, and the room is torn down only once
    # the call (the agent's own task) has finished. The exact interleaving of
    # "wait_for_agent_join" finishing and "handle_call" starting is a scheduling detail,
    # not a guarantee this test should pin down.
    assert index["make_admin_client"] < index["create_room"]
    assert index["create_room"] < index["wait_for_agent_join"]
    assert index["handle_call"] < index["dial_office"]
    assert index["wait_for_agent_join"] < index["dial_office"]
    assert index["dial_office"] < index["delete_room"]
    create_room_call = next(c for c in calls if c[0] == "create_room")
    assert create_room_call[1] == (CALL_ID,)
    assert create_room_call[2] == {"mode": "full_call", "case_id": CASE_ID, "record": True}
    dial_call = next(c for c in calls if c[0] == "dial_office")
    assert dial_call[1] == (CALL_ID, f"sip-{CALL_ID}")
    assert dial_call[2] == {
        "extension": "+15555550101",
        "trunk_name": "outbound-ivr-chime-outbound",
    }
    assert lkapi.closed is True


async def test_run_call_defaults_the_trunk_name_when_unset(monkeypatch):
    calls, _ = _wire_run_call(monkeypatch)
    monkeypatch.setenv("OUTBOUND_IVR_OFFICE_NUMBER", "+15555550101")
    monkeypatch.delenv("OUTBOUND_IVR_TRUNK_NAME", raising=False)

    await livekit_app._run_call(CALL_ID, CASE_ID)

    dial_call = next(c for c in calls if c[0] == "dial_office")
    assert dial_call[2]["trunk_name"] == "outbound-ivr-chime-outbound"


async def test_run_call_builds_the_transport_with_the_resolved_api_secret(monkeypatch):
    seen_transport = {}

    async def capture_handle_call(transport):
        seen_transport["api_secret"] = transport._api_secret
        seen_transport["room_name"] = transport._room_name
        seen_transport["identity"] = transport._identity

    _, _ = _wire_run_call(monkeypatch, handle_call=capture_handle_call)
    monkeypatch.setenv("OUTBOUND_IVR_OFFICE_NUMBER", "+15555550101")

    await livekit_app._run_call(CALL_ID, CASE_ID)

    assert seen_transport == {
        "api_secret": "resolved-secret",
        "room_name": CALL_ID,
        "identity": livekit_app.AGENT_IDENTITY,
    }


async def test_run_call_never_dials_if_the_agent_never_joins(monkeypatch):
    calls, lkapi = _wire_run_call(monkeypatch)
    monkeypatch.setenv("OUTBOUND_IVR_OFFICE_NUMBER", "+15555550101")

    async def never_joins(client, call_id, proc, identity, timeout):
        raise RuntimeError(f"Outbound IVR did not join room {call_id!r}")

    monkeypatch.setattr(livekit_app.livekit_calls, "wait_for_agent_join", never_joins)

    await livekit_app._run_call(CALL_ID, CASE_ID)  # must not raise out of _run_call

    assert "dial_office" not in [c[0] for c in calls]
    assert "delete_room" in [c[0] for c in calls]  # still cleaned up
    assert lkapi.closed is True


async def test_run_call_cancels_the_agent_task_if_dialing_fails(monkeypatch):
    joined = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_handle_call(transport):
        joined.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _, _ = _wire_run_call(monkeypatch, handle_call=slow_handle_call)
    monkeypatch.setenv("OUTBOUND_IVR_OFFICE_NUMBER", "+15555550101")

    async def failing_dial(client, call_id, sip_identity, **kw):
        raise RuntimeError("no trunk named that")

    monkeypatch.setattr(livekit_app.livekit_calls, "dial_office", failing_dial)

    await asyncio.wait_for(livekit_app._run_call(CALL_ID, CASE_ID), timeout=2)

    assert joined.is_set()
    assert cancelled.is_set()


async def test_run_call_requires_the_office_number_env_var(monkeypatch):
    calls, lkapi = _wire_run_call(monkeypatch)
    monkeypatch.delenv("OUTBOUND_IVR_OFFICE_NUMBER", raising=False)

    await livekit_app._run_call(CALL_ID, CASE_ID)  # must not raise out -- caught and logged

    assert "dial_office" not in [c[0] for c in calls]
    assert "delete_room" in [c[0] for c in calls]
    assert lkapi.closed is True


class _FakeTable:
    def __init__(self, items=None, item=None):
        self.items = items or []
        self.item = item
        self.updates = []

    def query(self, **_kwargs):
        return {"Items": self.items}

    def get_item(self, **_kwargs):
        return {"Item": self.item} if self.item else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


def _fake_dynamodb(monkeypatch, events_items, case=None):
    import boto3

    tables = {"events": _FakeTable(events_items), "cases": _FakeTable(item=case)}

    class _Resource:
        def Table(self, name):
            return tables[name]

    monkeypatch.setattr(boto3, "resource", lambda *_a, **_k: _Resource())
    monkeypatch.setenv("OUTBOUND_IVR_EVENTS_TABLE", "events")
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", "cases")
    return tables


def test_record_case_outcome_closes_the_case_with_the_last_handoff(monkeypatch):
    tables = _fake_dynamodb(
        monkeypatch,
        [
            {"type": "said"},
            {"type": "handoff", "outcome": "complete", "summary": "all three items"},
        ],
    )
    livekit_app._record_case_outcome("call-1", "case-1")
    (update,) = tables["cases"].updates
    values = update["ExpressionAttributeValues"]
    assert update["Key"] == {"case_id": "case-1"}
    assert values[":status"] == "complete"
    assert values[":outcome"] == {"type": "complete", "summary": "all three items"}
    assert "REMOVE current_call" in update["UpdateExpression"]
    assert "ADD attempts :one" in update["UpdateExpression"]


def test_record_case_outcome_returns_a_call_without_an_outcome_to_pending(monkeypatch):
    tables = _fake_dynamodb(monkeypatch, [{"type": "ended", "reason": "far end hung up"}])
    livekit_app._record_case_outcome("call-2", "case-1")
    (update,) = tables["cases"].updates
    assert update["ExpressionAttributeValues"][":status"] == "pending"
    assert ":outcome" not in update["ExpressionAttributeValues"]


def test_record_case_outcome_never_raises(monkeypatch):
    import boto3

    def broken(*_a, **_k):
        raise RuntimeError("no dynamodb")

    monkeypatch.setattr(boto3, "resource", broken)
    monkeypatch.setenv("OUTBOUND_IVR_EVENTS_TABLE", "events")
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", "cases")
    livekit_app._record_case_outcome("call-3", "case-1")


def test_log_telemetry_setup_reports_the_provider_without_header_values(monkeypatch):
    from structlog.testing import capture_logs

    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS", "x-aws-log-group=secret-ish")
    with capture_logs() as cap_logs:
        livekit_app._log_telemetry_setup()
    (entry,) = [log for log in cap_logs if log.get("event") == "telemetry_setup"]
    line = str(entry)
    assert "OTEL_TRACES_EXPORTER" in line
    assert "secret-ish" not in line


def test_watch_span_exports_logs_each_batch_and_the_startup_span(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from structlog.testing import capture_logs

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(trace, "get_tracer", lambda name, *a, **k: provider.get_tracer(name))
    with capture_logs() as cap_logs:
        livekit_app._watch_span_exports()
    entries = [log for log in cap_logs if log.get("event") == "telemetry_spans_exported"]
    assert any(
        e["exporter"] == "InMemorySpanExporter"
        and e["span_count"] == 1
        and e["span_names"] == ["outbound_ivr.startup"]
        and str(e["result"]) == "SUCCESS"
        for e in entries
    ), entries
    assert [s.name for s in exporter.get_finished_spans()] == ["outbound_ivr.startup"]


async def test_the_call_is_a_sampled_trace_even_when_the_invocation_is_not(monkeypatch):
    """The invocation arrives with an unsampled trace context; spans under it were all
    dropped, so the call starts its own sampled trace, linked to the invocation."""
    from opentelemetry import baggage, context, trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    provider = TracerProvider()  # ParentBased(AlwaysOn), the same as in the container
    monkeypatch.setattr(trace, "get_tracer", lambda name, *a, **k: provider.get_tracer(name))
    monkeypatch.setattr(livekit_app.app, "add_async_task", lambda name, metadata=None: 1)
    monkeypatch.setattr(livekit_app.app, "complete_async_task", lambda task_id: True)
    monkeypatch.setattr(livekit_app, "_record_case_outcome", lambda call_id, case_id: None)
    seen = {}

    async def fake_run_call(call_id, case_id):
        span = trace.get_current_span()
        seen["sampled"] = span.get_span_context().trace_flags.sampled
        seen["trace_id"] = span.get_span_context().trace_id
        seen["links"] = [link.context.trace_id for link in span.links]
        seen["session"] = span.attributes.get("session.id")

    monkeypatch.setattr(livekit_app, "_run_call", fake_run_call)
    unsampled = SpanContext(
        trace_id=0xABC, span_id=0xDEF, is_remote=True, trace_flags=TraceFlags(0)
    )
    ctx = trace.set_span_in_context(
        NonRecordingSpan(unsampled), baggage.set_baggage("session.id", "sess-1")
    )
    token = context.attach(ctx)
    try:
        await livekit_app._run_call_tracked(CALL_ID, CASE_ID)
    finally:
        context.detach(token)

    assert seen["sampled"] is True
    assert seen["trace_id"] != 0xABC
    assert seen["links"] == [0xABC]
    assert seen["session"] == "sess-1"


def test_evaluation_attributes_carry_the_task_and_the_call(monkeypatch):
    """AgentCore Evaluations rejected every call: no invoke_agent span with a prompt
    and a response. The task comes from the case; the response is the call itself."""
    _fake_dynamodb(
        monkeypatch,
        [
            {"type": "handoff", "outcome": "complete", "summary": "Collected all three.", "seq": 9},
            {"type": "heard", "text": "Press two for prior authorizations.", "seq": 1},
            {"type": "dtmf", "digit": "2", "option": "prior authorizations", "seq": 2},
            {"type": "said", "text": "Hello, this is Outbound IVR.", "seq": 3},
            {"type": "tool", "name": "record_item", "seq": 4},
        ],
        case={
            "provider_office": "AnyCompany Medical Group",
            "payer": "Example Health Plan",
            "pa_reference": "PA-1",
            "patient_name": "Pat Example",
            "missing_items": [{"key": "mri_date", "label": "MRI date"}, {"key": "bmi"}],
        },
    )
    attributes = livekit_app._evaluation_attributes("call-1", "case-1")
    task = attributes["gen_ai.task.input"]
    assert "AnyCompany Medical Group" in task and "MRI date, bmi" in task
    # Spans leave the app's own tables: no patient name or authorization reference.
    assert "Pat Example" not in task and "PA-1" not in task
    assert attributes["gen_ai.task.output"].splitlines() == [
        "Office: Press two for prior authorizations.",
        "[Outbound IVR pressed 2 for: prior authorizations]",
        "Outbound IVR: Hello, this is Outbound IVR.",
        "Outcome: complete. Collected all three.",
    ]
    assert attributes["agentcore.invocation.user_prompt"] == task


def test_evaluation_attributes_never_raise(monkeypatch):
    import boto3

    monkeypatch.setattr(
        boto3, "resource", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no dynamodb"))
    )
    monkeypatch.setenv("OUTBOUND_IVR_EVENTS_TABLE", "events")
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", "cases")
    assert livekit_app._evaluation_attributes("call-1", "case-1") == {}


async def test_the_call_span_is_an_invoke_agent_span_the_evaluations_can_read(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", lambda name, *a, **k: provider.get_tracer(name))
    monkeypatch.setattr(livekit_app.app, "add_async_task", lambda name, metadata=None: 1)
    monkeypatch.setattr(livekit_app.app, "complete_async_task", lambda task_id: True)
    monkeypatch.setattr(livekit_app, "_record_case_outcome", lambda call_id, case_id: None)
    monkeypatch.setattr(
        livekit_app,
        "_evaluation_attributes",
        lambda call_id, case_id: {"gen_ai.task.input": "the task"},
    )

    async def fake_run_call(call_id, case_id):
        return None

    monkeypatch.setattr(livekit_app, "_run_call", fake_run_call)
    await livekit_app._run_call_tracked(CALL_ID, CASE_ID)

    (span,) = exporter.get_finished_spans()
    assert span.instrumentation_scope.name.startswith("opentelemetry.instrumentation.")
    assert span.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert span.attributes["gen_ai.task.input"] == "the task"


def test_evaluation_attributes_without_call_content_keep_only_presses_and_outcome(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "0")
    _fake_dynamodb(
        monkeypatch,
        [
            {"type": "heard", "text": "Patient date of birth is March 14th.", "seq": 1},
            {"type": "dtmf", "digit": "3", "seq": 2},
            {"type": "said", "text": "Thank you.", "seq": 3},
            {"type": "handoff", "outcome": "complete", "summary": "Collected all three.", "seq": 4},
        ],
        case={"missing_items": []},
    )
    output = livekit_app._evaluation_attributes("call-1", "case-1")["gen_ai.task.output"]
    # The summary used to survive this flag. It is the model's own prose about the
    # conversation ("Collected all three." here, but it can name anything it
    # heard), so it is call content and now goes with the rest. The outcome label
    # is one of a fixed set and stays.
    assert output.splitlines() == ["[Outbound IVR pressed 3]", "Outcome: complete."]


# --------------------------------------------------------------------------
# Call content and the one switch that governs it. OUTBOUND_IVR_TRACE_CALL_CONTENT=0
# used to leave three leaks open: the transcript's quoted menu options, the
# model's free-text outcome summary, and every spoken line in the container log.
# --------------------------------------------------------------------------


def test_trace_call_content_off_redacts_the_quoted_menu_option(monkeypatch):
    """The digit is structure; the option quoted next to it is transcribed from
    the far end's audio, and it used to go out even with content tracing off."""
    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "0")
    _fake_dynamodb(
        monkeypatch,
        [
            {"type": "dtmf", "digit": "3", "option": "press three for provider services", "seq": 1},
            {"type": "handoff", "outcome": "complete", "summary": "Done.", "seq": 2},
        ],
        case={"missing_items": []},
    )
    output = livekit_app._evaluation_attributes("call-1", "case-1")["gen_ai.task.output"]
    assert "provider services" not in output
    assert output.splitlines()[0] == "[Outbound IVR pressed 3]"


def test_trace_call_content_on_keeps_the_quoted_menu_option(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "1")
    _fake_dynamodb(
        monkeypatch,
        [{"type": "dtmf", "digit": "3", "option": "press three for provider services", "seq": 1}],
        case={"missing_items": []},
    )
    output = livekit_app._evaluation_attributes("call-1", "case-1")["gen_ai.task.output"]
    assert "press three for provider services" in output


def test_event_log_redacts_content_fields_when_tracing_is_off(monkeypatch):
    from structlog.testing import capture_logs

    from outbound_ivr.voice.agent import EventLog

    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "0")
    log = EventLog("00000000-0000-4000-8000-000000000000", sink="stdout")
    with capture_logs() as cap_logs:
        log.write("heard", text="my date of birth is January third")
        log.write("dtmf", digit="3", option="press three for provider services")
        log.write("got", key="mri_date", value="2026-03-14", confirmed=True)

    logged = "\n".join(str(log) for log in cap_logs)
    assert "January third" not in logged
    assert "press three" not in logged
    assert "2026-03-14" not in logged
    # Structure survives, so the log is still usable for debugging.
    assert "digit=3" in logged
    assert "key=mri_date" in logged
    assert "confirmed=True" in logged
    assert "<redacted" in logged


def test_event_log_keeps_content_when_tracing_is_on(monkeypatch):
    from structlog.testing import capture_logs

    from outbound_ivr.voice.agent import EventLog

    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "1")
    log = EventLog("00000000-0000-4000-8000-000000000001", sink="stdout")
    with capture_logs() as cap_logs:
        log.write("heard", text="my date of birth is January third")

    assert "January third" in "\n".join(str(log) for log in cap_logs)


# --------------------------------------------------------------------------
# The stdout path. On the AgentCore Runtime, EventLog's sink is "stdout" and
# stdout IS the log: it goes straight to CloudWatch. That line used to print the
# raw event record, so the patient's name, DOB and member ID went to CloudWatch
# on every event no matter what OUTBOUND_IVR_TRACE_CALL_CONTENT said. The redaction
# test that existed used caplog, which never sees print(), which is exactly how
# the gap survived a passing suite. These use capsys.
# --------------------------------------------------------------------------


def test_stdout_sink_redacts_call_content_when_tracing_is_off(monkeypatch, capsys):
    from outbound_ivr.voice.agent import EventLog

    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "0")
    log = EventLog("00000000-0000-4000-8000-00000000000a", sink="stdout")
    log.write("heard", text="this is Jane Qpublic, date of birth March fourteenth")
    log.write("got", key="mri_date", value="2026-03-14", confirmed=True)

    out = capsys.readouterr().out
    assert "Jane Qpublic" not in out
    assert "March fourteenth" not in out
    assert "2026-03-14" not in out
    assert "<redacted" in out
    # Structure survives, so the log is still usable.
    assert '"type": "got"' in out or '"type":"got"' in out
    assert "mri_date" in out


def test_stdout_sink_redacts_the_ready_events_whole_case(monkeypatch, capsys):
    """`ready` carries the case summary and checklist, which hold the patient's
    name, DOB and member ID. It is excluded from the logger.info line entirely,
    so stdout was the only place it appeared, unredacted."""
    from outbound_ivr.voice.agent import EventLog

    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "0")
    log = EventLog("00000000-0000-4000-8000-00000000000b", sink="stdout")
    log.write(
        "ready",
        mode="full_call",
        case={"patient_name": "Jane Qpublic", "patient_dob": "1990-03-14", "member_id": "M-123"},
        checklist=[{"key": "mri_date", "label": "Date of the MRI"}],
    )

    out = capsys.readouterr().out
    assert "Jane Qpublic" not in out
    assert "1990-03-14" not in out
    assert "M-123" not in out
    assert "full_call" in out, "the non-content fields still have to be there"


def test_stdout_sink_keeps_content_when_tracing_is_on(monkeypatch, capsys):
    from outbound_ivr.voice.agent import EventLog

    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "1")
    log = EventLog("00000000-0000-4000-8000-00000000000c", sink="stdout")
    log.write("heard", text="this is Jane Qpublic")
    assert "Jane Qpublic" in capsys.readouterr().out


def test_the_file_sink_and_the_forward_keep_the_full_record(monkeypatch, tmp_path):
    """Only stdout is a log. The events file and the DynamoDB forward are the
    event store the dashboard renders, so redacting those would break the product
    rather than protect anything."""
    from outbound_ivr.voice.agent import EventLog

    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "0")
    import outbound_ivr.voice.agent as agent_module

    monkeypatch.setattr(agent_module, "RUNS", tmp_path)
    forwarded: list[dict] = []
    log = EventLog("00000000-0000-4000-8000-00000000000d", sink="file", forward=forwarded.append)
    log.write("heard", text="this is Jane Qpublic")
    log.close()

    assert "Jane Qpublic" in log.path.read_text()
    assert forwarded[0]["text"] == "this is Jane Qpublic"


# --------------------------------------------------------------------------
# Nested content and free-standing log lines. A `tool` event carries the tool
# call's own arguments as `input`, so record_item's recorded answer is
# input["value"], one level below where the top-level redaction looked. And three
# logger lines in CallState.say / the response tracker printed the spoken text
# directly instead of going through EventLog at all.
# --------------------------------------------------------------------------


def test_tool_event_input_is_redacted_on_both_log_paths(monkeypatch, capsys):
    from structlog.testing import capture_logs

    from outbound_ivr.voice.agent import EventLog

    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "0")
    log = EventLog("00000000-0000-4000-8000-00000000000c", sink="stdout")
    with capture_logs() as cap_logs:
        log.write(
            "tool",
            tool="record_item",
            status="success",
            ms=12,
            input={"key": "mri_date", "value": "2026-03-14", "confirmed": True},
        )
        log.write(
            "tool",
            tool="press_key",
            status="success",
            ms=3,
            input={"digit": "3", "option": "press three for provider services"},
        )

    logged = "\n".join(str(log) for log in cap_logs)
    out = capsys.readouterr().out
    for text in (logged, out):
        assert "2026-03-14" not in text
        assert "press three" not in text
        assert "mri_date" in text, "the structure must survive"
        assert "<redacted" in text


def test_say_does_not_log_stage_directions_or_narration_when_tracing_is_off(monkeypatch):
    from structlog.testing import capture_logs

    from outbound_ivr.voice.agent import CallState, EventLog

    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "0")
    state = CallState(
        call_uuid="00000000-0000-4000-8000-00000000000d",
        mode="intake",
        log=EventLog("00000000-0000-4000-8000-00000000000d", sink="stdout"),
    )
    with capture_logs() as cap_logs:
        state.say("(silence while Jane Qpublic's file loads)")
        state.say("[Thinking Process: confirm the date of birth 1990-03-14]")

    logged = "\n".join(str(log) for log in cap_logs)
    assert "Jane Qpublic" not in logged
    assert "1990-03-14" not in logged


def test_say_logs_the_text_when_tracing_is_on(monkeypatch):
    from structlog.testing import capture_logs

    from outbound_ivr.voice.agent import CallState, EventLog

    monkeypatch.setenv("OUTBOUND_IVR_TRACE_CALL_CONTENT", "1")
    state = CallState(
        call_uuid="00000000-0000-4000-8000-00000000000e",
        mode="intake",
        log=EventLog("00000000-0000-4000-8000-00000000000e", sink="stdout"),
    )
    with capture_logs() as cap_logs:
        state.say("(silence while the file loads)")

    assert "the file loads" in "\n".join(str(log) for log in cap_logs)
