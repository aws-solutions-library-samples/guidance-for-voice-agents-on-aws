"""Bedrock AgentCore Runtime entrypoint: starts one outbound LiveKit call in the
background and returns at once.

The dashboard's `POST /api/calls` Lambda starts a call by calling `InvokeAgentRuntime`
on this Runtime once, with a plain JSON payload (`{"action": "start_call", "call_id":
"...", "case_id": "..."}`) and a `runtimeSessionId` of `call_id` padded to at least 33
characters. An AgentCore invoke expects a response quickly, not a call lasting minutes,
so `start_call` below kicks off the actual call -- create/join the LiveKit room, run
`outbound_ivr.voice.agent.handle_call`, dial the office -- as a background `asyncio.Task` and
returns `{"call": call_id, "status": "started"}` right away. `app.add_async_task`/
`complete_async_task` (bedrock-agentcore's own health-tracking primitives, confirmed
against the installed 1.24.1 SDK -- see pyproject.toml's `agentcore` extra) mark the
Runtime `HealthyBusy` for exactly as long as that task is running, so the platform's own
idle/health check never recycles the container mid-call.

Synthetic data only. Adapting this to real patient data makes it PHI, and handling PHI is
the deployer's responsibility under the AWS shared responsibility model: a HIPAA-eligible
service configuration, a Business Associate Agreement with AWS, and the deployer's own
compliance review, none of which this repository provides. Call content
reaches traces and logs only when OUTBOUND_IVR_TRACE_CALL_CONTENT opts in (see README.md,
"Security and data").

Local run (no AgentCore, no AWS; needs a LiveKit server reachable at LIVEKIT_URL and
OUTBOUND_IVR_OFFICE_NUMBER/OUTBOUND_IVR_TRUNK_NAME pointed at a real or test trunk):

    uv run --extra strands --extra agentcore --extra livekit python agentcore/livekit_app.py
    curl -s -X POST http://127.0.0.1:8080/invocations \\
      -H 'Content-Type: application/json' \\
      -d '{"action": "start_call", "call_id": "<uuid>", "case_id": "<case id>"}'
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime
from typing import Any

import structlog
from bedrock_agentcore.runtime.app import BedrockAgentCoreApp
from dotenv import load_dotenv

load_dotenv()

from outbound_ivr.voice import (
    agent,
    livekit_calls,
)
from outbound_ivr.voice.livekit_transport import LiveKitTransport

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger("outbound_ivr.agentcore.livekit")

app = BedrockAgentCoreApp()

AGENT_IDENTITY = "outbound_ivr"
# Outbound IVR always dials out and works the phone menu itself by listening -- there is no
# per-call "mode" to pick.
CALL_MODE = "full_call"

# Default outbound trunk name, matching outbound_ivr.voice.backend.DEFAULT_TRUNK_NAME (the
# Evidence-team leg's own default) -- both legs go out through the one deployed trunk.
DEFAULT_TRUNK_NAME = "outbound-ivr-chime-outbound"


class _TaskAsProcess:
    """Adapts an `asyncio.Task` to the `.returncode` shape
    `outbound_ivr.voice.livekit_calls.wait_for_agent_join` already checks, so this entrypoint
    can reuse that polling loop unchanged instead of duplicating it: inside this
    Runtime container the call *is* this process, so `handle_call` runs as an
    in-process `asyncio.Task`, and this is what can "exit" before joining.
    """

    def __init__(self, task: asyncio.Task) -> None:
        self._task = task

    @property
    def returncode(self) -> int | None:
        if not self._task.done():
            return None
        return 1 if self._task.exception() is not None else 0


async def _run_call(call_id: str, case_id: str) -> None:
    """Create the room, run Outbound IVR in-process, and dial the office through the deployed
    trunk -- the same sequencing as `outbound_ivr.voice.livekit_calls._run_call` (room, then
    agent, then dial, waiting for the agent to join before dialing out), minus the
    subprocess-specific pieces (`spawn_agent`, a log file) that don't apply inside one
    Runtime container running the call directly.
    """
    lkapi = livekit_calls.make_admin_client()
    sip_identity = f"sip-{call_id}"
    transport = LiveKitTransport(
        api_secret=livekit_calls.resolve_api_secret(),
        room_name=call_id,
        sip_identity=sip_identity,
        identity=AGENT_IDENTITY,
    )
    call_task: asyncio.Task | None = None
    try:
        await livekit_calls.create_room(
            lkapi, call_id, mode=CALL_MODE, case_id=case_id, record=True
        )
        call_task = asyncio.create_task(agent.handle_call(transport))
        await livekit_calls.wait_for_agent_join(
            lkapi, call_id, _TaskAsProcess(call_task), AGENT_IDENTITY, livekit_calls.JOIN_WAIT_S
        )
        office_number = os.environ["OUTBOUND_IVR_OFFICE_NUMBER"]
        trunk_name = os.environ.get("OUTBOUND_IVR_TRUNK_NAME", DEFAULT_TRUNK_NAME)
        await livekit_calls.dial_office(
            lkapi, call_id, sip_identity, extension=office_number, trunk_name=trunk_name
        )
        await asyncio.to_thread(_mark_case_calling, call_id, case_id)
        await asyncio.wait_for(call_task, timeout=livekit_calls.CALL_TIMEOUT_S)
    except Exception:
        logger.exception("livekit_call_ended_abnormally", call_id=call_id)
    finally:
        if call_task is not None:
            # On an error, the call ceiling, or this task itself being cancelled
            # (which `except Exception` doesn't catch), the agent must stop too, or
            # the await below waits on a call nobody is running any more.
            if not call_task.done():
                call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await call_task
        with contextlib.suppress(Exception):
            await livekit_calls.delete_room(lkapi, call_id)
        with contextlib.suppress(Exception):
            await lkapi.aclose()


# The call's final "handoff" event outcome -> the case status the dashboard shows.
_CASE_STATUS = {"complete": "complete", "escalated": "escalated", "handed_off": "escalated"}

# The case lifecycle around one call: the dashboard's Start call claims the case as
# "queued" with `current_call = <call id>` (lambdas/api/handler.py, `_mark_case_queued`);
# `_mark_case_calling` flips it to "calling" once the office has been dialed; and
# `_record_case_outcome` always moves it out of "calling" when the call ends, however
# it ends. Both Runtime writes are conditioned on this call still owning the case, so a
# call that outlives a reset and a fresh dial never overwrites the newer call's claim.


def _is_conditional_check_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get("Code") == "ConditionalCheckFailedException"


def _mark_case_calling(call_id: str, case_id: str) -> None:
    """Flip the case from "queued" to "calling" once the office has been dialed, so the
    dashboard's edit lock and Start call guard see a call in flight. Only if this call
    still holds the claim; otherwise (an operator reset the case in the meantime) the
    case is left as it is. Never raises: a failure here leaves the case "queued", which
    the outcome write at call end closes out the same way."""
    cases_table = os.environ.get("OUTBOUND_IVR_CASES_TABLE")
    if not cases_table:
        return
    try:
        import boto3

        boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"]).Table(
            cases_table
        ).update_item(
            Key={"case_id": case_id},
            UpdateExpression="SET #status = :calling, updated_at = :now",
            ConditionExpression="#status = :queued AND current_call = :call",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":calling": "calling",
                ":queued": "queued",
                ":call": call_id,
                ":now": datetime.now(UTC).isoformat(),
            },
        )
    except Exception as exc:
        if _is_conditional_check_failure(exc):
            logger.warning("case_calling_skipped_claim_moved", call_id=call_id, case_id=case_id)
        else:
            logger.exception("case_calling_mark_failed", call_id=call_id, case_id=case_id)


def _last_handoff(dynamodb: Any, events_table: str, call_id: str) -> dict | None:
    from boto3.dynamodb.conditions import Key

    handoffs = [
        item
        for item in dynamodb.Table(events_table)
        .query(KeyConditionExpression=Key("call").eq(call_id))
        .get("Items", [])
        if item.get("type") == "handoff"
    ]
    return handoffs[-1] if handoffs else None


def _record_case_outcome(call_id: str, case_id: str) -> None:
    """Close out the case item the dashboard's Start call claimed: its tools run
    in-process here, so nothing else updates the cases table. Reads the call's last
    handoff event from the events table; a call that ended without one, or whose
    events can't be read, goes back to "pending" so it can be retried.

    This runs after every call however it ended (normally, on the call ceiling,
    abnormally, cancelled), and it is the only thing that moves a case out of
    "calling", so it tries twice: if the full write fails, a minimal one without the
    outcome still hands the case back. Conditioned on this call still owning the case
    (`current_call` is this call, or gone after an operator reset), so a newer call's
    claim is left alone. Never raises: the call is already over."""
    cases_table = os.environ.get("OUTBOUND_IVR_CASES_TABLE")
    events_table = os.environ.get("OUTBOUND_IVR_EVENTS_TABLE")
    if not cases_table or not events_table:
        return
    try:
        import boto3

        dynamodb = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"])
        cases = dynamodb.Table(cases_table)
    except Exception:
        logger.exception("case_outcome_record_failed", call_id=call_id)
        return
    try:
        last = _last_handoff(dynamodb, events_table, call_id)
    except Exception:
        logger.exception("case_outcome_events_read_failed", call_id=call_id)
        last = None
    status = _CASE_STATUS.get(last.get("outcome")) if last else None
    outcome = (
        {k: v for k, v in last.items() if k in ("reason", "summary", "follow_up")}
        | {"type": last["outcome"]}
        if last and status
        else None
    )
    values = {
        ":status": status or "pending",
        ":now": datetime.now(UTC).isoformat(),
        ":call": call_id,
        ":one": 1,
    }
    expression = (
        "SET #status = :status, updated_at = :now, last_call = :call"
        " ADD attempts :one REMOVE current_call"
    )
    minimal = (expression, values)
    full = (
        (expression.replace(" ADD", ", outcome = :outcome ADD"), values | {":outcome": outcome})
        if outcome
        else minimal
    )
    # The second try drops the outcome map (the case detail falls back to the S3 case
    # record for it): what matters most is that the case leaves "calling".
    for update, update_values in (full, minimal):
        try:
            cases.update_item(
                Key={"case_id": case_id},
                UpdateExpression=update,
                ConditionExpression="attribute_not_exists(current_call) OR current_call = :call",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=update_values,
            )
            return
        except Exception as exc:
            if _is_conditional_check_failure(exc):
                # A newer call holds the case; it closes the case out itself.
                logger.warning("case_outcome_skipped_claim_moved", call_id=call_id, case_id=case_id)
                return
            logger.exception("case_outcome_record_failed", call_id=call_id)
    logger.error("case_outcome_record_failed_manual_reset_needed", call_id=call_id, case_id=case_id)


# AgentCore Evaluations reads a call through one "invoke agent" span: the task as the
# prompt, the call as the response. Strands' speech-to-speech agent emits session and
# response spans but no such span, so the call's root span carries it, under a scope
# name the service reads with its generic OpenTelemetry handling.
EVALUATION_SCOPE = "opentelemetry.instrumentation.outbound_ivr"
# Kept well under the span size limits; a few minutes of call is a few KB.
_TRANSCRIPT_LIMIT = 12_000


def _evaluation_attributes(call_id: str, case_id: str) -> dict[str, str]:
    """The task (from the case) and what happened (the call's transcript, key presses
    and outcome, from the events table), for the root span. Never raises."""
    cases_table = os.environ.get("OUTBOUND_IVR_CASES_TABLE")
    events_table = os.environ.get("OUTBOUND_IVR_EVENTS_TABLE")
    if not cases_table or not events_table:
        return {}
    try:
        import boto3
        from boto3.dynamodb.conditions import Key

        dynamodb = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"])
        case = dynamodb.Table(cases_table).get_item(Key={"case_id": case_id}).get("Item") or {}
        events = (
            dynamodb.Table(events_table)
            .query(KeyConditionExpression=Key("call").eq(call_id))
            .get("Items", [])
        )
    except Exception:
        logger.exception("call_read_for_evaluation_failed", call_id=call_id)
        return {}
    items = ", ".join(
        item.get("label") or item.get("key", "") for item in case.get("missing_items", [])
    )
    # `task` carries no patient name and no authorization reference: office, payer
    # and item labels only. That is true of THIS attribute and not of the function
    # -- `gen_ai.task.output` below is the call transcript, and Outbound IVR says the
    # patient's name out loud in its opening line, so the transcript has it. The
    # comment here used to read as though it covered everything the function
    # returns. What actually keeps call content off the spans is
    # OUTBOUND_IVR_TRACE_CALL_CONTENT=0, which the deploy sets from
    # `-c traceCallContent=false`.
    task = (
        f"Call {case.get('provider_office') or 'the provider office'} for {case.get('payer') or 'the payer'} "
        "about a pending prior authorization. Get through the phone menu to the prior "
        f"authorization desk, then collect and confirm each missing item: {items or 'none listed'}. "
        "If the office can't help, offer a warm transfer to a person, or escalate."
    )
    # The spoken lines carry whatever is said on the call. On by default for the
    # synthetic demo; OUTBOUND_IVR_TRACE_CALL_CONTENT=0 keeps only key presses and the outcome.
    spoken = os.environ.get("OUTBOUND_IVR_TRACE_CALL_CONTENT", "1").lower() not in (
        "0",
        "false",
        "no",
    )
    lines = []
    outcome = None
    for event in sorted(events, key=lambda e: int(e.get("seq", 0))):
        kind = event.get("type")
        if kind == "said" and event.get("text") and spoken:
            lines.append(f"Outbound IVR: {event['text']}")
        elif kind == "heard" and event.get("text") and spoken:
            lines.append(f"Office: {event['text']}")
        elif kind == "dtmf" and event.get("digit"):
            # The digit is not call content; the quoted menu option is, since it
            # is transcribed from what the far end said.
            option = f" for: {event['option']}" if event.get("option") and spoken else ""
            lines.append(f"[Outbound IVR pressed {event['digit']}{option}]")
        elif kind == "handoff":
            outcome = event
    if outcome:
        # `outcome` is one of a fixed set of labels, so it stays either way. The
        # summary and reason are model-authored free text about the conversation,
        # which is call content and used to go out regardless of the flag.
        detail = (outcome.get("summary") or outcome.get("reason") or "") if spoken else ""
        lines.append(f"Outcome: {outcome.get('outcome')}. {detail}".strip())
    response = "\n".join(lines)[-_TRANSCRIPT_LIMIT:]
    return {
        "gen_ai.task.input": task,
        "gen_ai.task.output": response,
        "agentcore.invocation.user_prompt": task,
        "agentcore.invocation.agent_response": response,
    }


async def _run_call_tracked(call_id: str, case_id: str) -> None:
    """Wraps `_run_call` with the app's own async-task bookkeeping (module docstring):
    `HealthyBusy` for exactly as long as the call is running, and the failure (if any)
    always lands in CloudWatch Logs even though `start_call` has already returned."""
    from opentelemetry import baggage, trace

    task_id = app.add_async_task("call", {"call_id": call_id})
    # The call is its own trace, linked to the invocation rather than under it: the
    # invocation's trace context arrives not sampled, and the parent-based sampler
    # drops every span beneath an unsampled parent (Strands' included). Baggage, with
    # AgentCore's session.id, carries over, so the call's spans still group by session.
    invocation = trace.get_current_span().get_span_context()
    with trace.get_tracer(EVALUATION_SCOPE).start_as_current_span(
        "invoke_agent Outbound IVR",
        context=trace.set_span_in_context(trace.INVALID_SPAN),
        links=[trace.Link(invocation)] if invocation.is_valid else [],
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "Outbound IVR",
            "gen_ai.provider.name": "aws.bedrock",
            "gen_ai.request.model": os.environ.get("OUTBOUND_IVR_SONIC_MODEL", ""),
            "session.id": str(baggage.get_baggage("session.id") or call_id),
            "outbound_ivr.call_id": call_id,
            "outbound_ivr.case_id": case_id,
        },
    ) as span:
        try:
            await _run_call(call_id, case_id)
        except Exception:
            logger.exception("background_call_task_failed", call_id=call_id)
        finally:
            await asyncio.to_thread(_record_case_outcome, call_id, case_id)
            span.set_attributes(await asyncio.to_thread(_evaluation_attributes, call_id, case_id))
            app.complete_async_task(task_id)


@app.entrypoint
async def start_call(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """`{"action": "start_call", "call_id": "...", "case_id": "..."}` -- starts the call
    in the background and returns `{"call": call_id, "status": "started"}` at once.
    Any other `action` is rejected (there is only one today), so a malformed invoke
    fails loudly in the response instead of silently doing nothing.
    """
    payload = payload or {}
    action = payload.get("action")
    if action != "start_call":
        return {"error": f"unsupported action {action!r}; expected 'start_call'"}
    call_id = payload.get("call_id")
    case_id = payload.get("case_id")
    if not call_id or not case_id:
        return {"error": "call_id and case_id are both required"}

    session_id = getattr(context, "session_id", None)
    logger.info("call_starting", call_id=call_id, case_id=case_id, runtime_session_id=session_id)
    asyncio.create_task(_run_call_tracked(call_id, case_id))
    return {"call": call_id, "status": "started"}


def _log_telemetry_setup() -> None:
    """One line at startup saying whether spans can leave the container: the tracer
    provider, sampler and span exporters `opentelemetry-instrument` set up, and the
    OTEL_* settings AgentCore injected (header values left out)."""
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    processors = getattr(getattr(provider, "_active_span_processor", None), "_span_processors", ())
    exporters = [
        type(getattr(p, "span_exporter", None) or getattr(p, "_exporter", None)).__name__
        for p in processors
    ]
    settings = {
        k: v for k, v in sorted(os.environ.items()) if k.startswith("OTEL_") and "HEADERS" not in k
    }
    logger.info(
        "telemetry_setup",
        provider=type(provider).__name__,
        sampler=getattr(getattr(provider, "sampler", None), "get_description", lambda: None)(),
        exporters=exporters,
        agent_observability_enabled=os.environ.get("AGENT_OBSERVABILITY_ENABLED"),
        otel=settings,
    )


def _watch_span_exports() -> None:
    """Log each span batch the exporters send and what the endpoint answered, then send
    one startup span, so a deploy shows within seconds whether spans get out at all."""
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    processors = getattr(getattr(provider, "_active_span_processor", None), "_span_processors", ())
    for processor in processors:
        exporter = getattr(processor, "span_exporter", None) or getattr(
            processor, "_exporter", None
        )
        if exporter is None or getattr(exporter, "_outbound_ivr_watched", False):
            continue
        original = exporter.export
        exporter_name = type(exporter).__name__

        def export(spans, _original=original, _name=exporter_name):
            result = _original(spans)
            logger.debug(
                "telemetry_spans_exported",
                exporter=_name,
                span_count=len(spans),
                span_names=sorted({span.name for span in spans})[:8],
                result=getattr(result, "name", result),
            )
            return result

        exporter.export = export
        exporter._outbound_ivr_watched = True
    with trace.get_tracer("outbound_ivr.agentcore").start_as_current_span("outbound_ivr.startup"):
        pass
    flush = getattr(provider, "force_flush", None)
    logger.info("telemetry_startup_span_flushed", flushed=flush(10_000) if flush else None)


if __name__ == "__main__":
    agent._configure_telemetry()
    _log_telemetry_setup()
    _watch_span_exports()
    port = int(os.environ.get("PORT", "8080"))
    logger.info("agentcore_listening", port=port, path="/invocations")
    app.run(port=port)
