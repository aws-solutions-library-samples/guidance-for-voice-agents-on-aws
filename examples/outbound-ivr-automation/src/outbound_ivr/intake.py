"""Intake rules: recording items, escalating, and completing a prior-auth intake.

Framework-free on purpose: the Strands agent's tools (outbound_ivr.voice.agent) wrap these
functions, so the rules live in one place and can be unit tested without a model.
"""

from datetime import UTC, datetime

from outbound_ivr import prompts

ITEM_KEYS = [i["key"] for i in prompts.CASE["missing_items"]]


def new_record() -> dict:
    """Empty case record, the shape DynamoDB will hold later."""
    return {
        "pa_reference": prompts.CASE["pa_reference"],
        "patient": prompts.CASE["patient_name"],
        "started_at": datetime.now(UTC).isoformat(),
        "items": {},
        "outcome": None,
        "transcript": [],
    }


def record_item(record: dict, key: str, value: str, confirmed: bool) -> dict:
    """Store one answer. Returns the stored item."""
    item = {"value": value, "confirmed": bool(confirmed), "at": datetime.now(UTC).isoformat()}
    record["items"][key] = item
    return {"key": key, **item}


def use_case(record: dict, case: dict) -> None:
    """Point the record at the case actually being called: its checklist keys, auth
    reference and patient, instead of the fixed demo case."""
    record["item_keys"] = [i["key"] for i in case.get("missing_items", [])]
    record["pa_reference"] = case.get("pa_reference") or record["pa_reference"]
    record["patient"] = case.get("patient_name") or record["patient"]


def item_keys(record: dict) -> list[str]:
    """The checklist keys this record is collecting."""
    return record.get("item_keys") or ITEM_KEYS


def missing(record: dict) -> list[str]:
    """Items not yet recorded with a confirmed value."""
    return [k for k in item_keys(record) if not record["items"].get(k, {}).get("confirmed")]


def escalate(record: dict, reason: str, follow_up: str | None = None) -> dict:
    """Hand the call to a human. Returns the outcome."""
    record["outcome"] = {"type": "escalated", "reason": reason}
    if follow_up:
        record["outcome"]["follow_up"] = follow_up
    return record["outcome"]


def transfer_to_team(record: dict, reason: str, summary: str) -> dict:
    """Record a request to bring a human onto the call (LiveKit mode only, docs/
    escalation-handoff.md). Not a final outcome the way `escalate`/`complete` set one --
    the call is still going -- so this doesn't touch `record["outcome"]`."""
    entry = {"reason": reason, "summary": summary, "at": datetime.now(UTC).isoformat()}
    record.setdefault("handoff_requests", []).append(entry)
    return entry


def mark_handoff_joined(record: dict, reason: str, summary: str, identity: str) -> dict:
    """Promote a `transfer_to_team` request to its final outcome once a `"team-"`
    participant actually joins the room (docs/escalation-handoff.md, "What happens on
    join"). Not called when no one answers -- that path goes to `escalate` instead."""
    record["outcome"] = {
        "type": "handed_off",
        "reason": reason,
        "summary": summary,
        "joined_identity": identity,
        "joined_at": datetime.now(UTC).isoformat(),
    }
    return record["outcome"]


def complete(record: dict, summary: str) -> dict:
    """Close the call, unless items are missing, in which case it escalates instead.

    Don't take the model's word that the case is done: a case with items still missing
    goes to a human, whatever the model decided.
    """
    gaps = missing(record)
    if gaps:
        record["outcome"] = {
            "type": "escalated",
            "reason": "incomplete at close",
            "missing": gaps,
            "model_summary": summary,
        }
    else:
        record["outcome"] = {"type": "complete", "summary": summary}
    return record["outcome"]
