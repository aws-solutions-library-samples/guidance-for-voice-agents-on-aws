"""Lambda target behind the AgentCore Gateway "outbound-ivr-tools": record_item, escalate,
complete, transfer_to_team.

See docs/design.md, "Tools", and lambdas/README.md for how this gets bundled.

Event format (verified against AWS's own Lambda-target examples, October 2026): the
Gateway invokes this function once per tool call, passing the tool's input fields
directly as the Lambda event (no envelope) and the tool name on the Lambda context,
not in the event: `context.client_context.custom["bedrockAgentCoreToolName"]`, prefixed
with the Gateway target's name and three underscores, e.g. `"OutboundIvrTools___record_item"`.
Strip the prefix before dispatching.
"""

from __future__ import annotations

import os
from typing import Any

from outbound_ivr.tools.business import complete as _complete
from outbound_ivr.tools.business import escalate as _escalate
from outbound_ivr.tools.business import record_item as _record_item
from outbound_ivr.tools.business import transfer_to_team as _transfer_to_team
from outbound_ivr.tools.store import CaseStore, DynamoDBCaseStore

_store: CaseStore | None = None


def _get_store() -> CaseStore:
    global _store
    if _store is None:
        _store = DynamoDBCaseStore(
            cases_table=os.environ.get("OUTBOUND_IVR_CASES_TABLE", "outbound-ivr-cases"),
            offices_table=os.environ.get("OUTBOUND_IVR_OFFICES_TABLE", "outbound-ivr-offices"),
            endpoint_url=os.environ.get("OUTBOUND_IVR_DYNAMODB_ENDPOINT"),
        )
    return _store


def _tool_record_item(store: CaseStore, event: dict) -> str:
    return _record_item(
        store,
        case_id=event["case_id"],
        call=event.get("call", ""),
        key=event["key"],
        value=event["value"],
        confirmed=bool(event.get("confirmed", False)),
    )


def _tool_escalate(store: CaseStore, event: dict) -> str:
    return _escalate(
        store,
        case_id=event["case_id"],
        call=event.get("call", ""),
        reason=event["reason"],
        follow_up=event.get("follow_up") or None,
    )


def _tool_complete(store: CaseStore, event: dict) -> str:
    return _complete(
        store,
        case_id=event["case_id"],
        call=event.get("call", ""),
        summary=event["summary"],
    )


def _tool_transfer_to_team(store: CaseStore, event: dict) -> str:
    return _transfer_to_team(
        store,
        case_id=event["case_id"],
        call=event.get("call", ""),
        reason=event["reason"],
        summary=event["summary"],
    )


TOOL_HANDLERS = {
    "record_item": _tool_record_item,
    "escalate": _tool_escalate,
    "complete": _tool_complete,
    "transfer_to_team": _tool_transfer_to_team,
}


def tool_name_from_context(context: Any) -> str:
    """The bare tool name, with the Gateway's `{target}___` prefix stripped."""
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName", "") or ""
    if "___" in name:
        name = name.split("___", 1)[1]
    return name


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    tool_name = tool_name_from_context(context)
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool '{tool_name}'."}
    store = _get_store()
    try:
        result = handler(store, event)
    except KeyError as exc:
        return {"error": f"Missing required field: {exc.args[0]}"}
    return {"result": result}
