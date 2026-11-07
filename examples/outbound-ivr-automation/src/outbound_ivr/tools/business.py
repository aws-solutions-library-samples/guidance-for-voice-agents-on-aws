"""The business tools: record_item, escalate, complete, transfer_to_team.

Plain functions over a `CaseStore` (see store.py). These apply the same rules as
`outbound_ivr.intake` -- record_item only for a key on *this case's* missing items, complete
with items still missing becomes escalated -- but against a case loaded from the store
instead of the fixed `prompts.CASE`. They back three callers: the agent's in-process
tools (`OUTBOUND_IVR_TOOLS=local`), the Lambda handlers behind the AgentCore Gateway, and
tests. Return values are short strings for the model, matching
`outbound_ivr.voice.agent`'s existing tool results.
"""

from __future__ import annotations

from datetime import UTC, datetime

from outbound_ivr.tools.store import CaseStore

UNKNOWN_CASE = "Unknown case."
NO_FINAL_OUTCOME = "This case already has a final outcome."


def _missing_keys(case: dict) -> list[str]:
    results = case.get("results") or {}
    return [
        item["key"]
        for item in case.get("missing_items", [])
        if not results.get(item["key"], {}).get("confirmed")
    ]


def record_item(
    store: CaseStore, case_id: str, call: str, key: str, value: str, confirmed: bool
) -> str:
    """Save one confirmed answer, only if `key` is one of this case's missing items."""
    case = store.get_case(case_id)
    if case is None:
        return UNKNOWN_CASE
    allowed = {item["key"] for item in case.get("missing_items", [])}
    if key not in allowed:
        return f"'{key}' is not one of this case's missing items."
    store.record_result(case_id, call=call, key=key, value=value, confirmed=bool(confirmed))
    return "Saved."


def escalate(
    store: CaseStore, case_id: str, call: str, reason: str, follow_up: str | None = None
) -> str:
    """Hand the call to a human. `reason` must be non-empty."""
    if not reason or not reason.strip():
        return "A reason is required to escalate."
    case = store.get_case(case_id)
    if case is None:
        return UNKNOWN_CASE
    outcome: dict = {"type": "escalated", "reason": reason}
    if follow_up:
        outcome["follow_up"] = follow_up
    if not store.set_outcome(case_id, outcome, status="escalated"):
        return NO_FINAL_OUTCOME
    return "Escalated. Thank them in one short sentence, say goodbye, and stop talking."


def transfer_to_team(store: CaseStore, case_id: str, call: str, reason: str, summary: str) -> str:
    """Request a warm handoff to a human colleague (LiveKit mode only). `reason` must be
    non-empty, same rule as `escalate`.

    Records an outcome-in-progress, status `"handoff_requested"` -- not yet a final
    outcome the way `escalate`/`complete` set one, since the call isn't over: a human
    may still join (see `mark_handoff_joined`), or the model may fall back to `escalate`
    if no one answers. `store.set_outcome`'s "already has a final outcome" guard must
    not block either of those on the same case.
    """
    if not reason or not reason.strip():
        return "A reason is required to transfer to the team."
    case = store.get_case(case_id)
    if case is None:
        return UNKNOWN_CASE
    outcome = {"type": "transfer_to_team", "reason": reason, "summary": summary}
    if not store.set_outcome(case_id, outcome, status="handoff_requested"):
        return NO_FINAL_OUTCOME
    return "Requested."


def mark_handoff_joined(
    store: CaseStore, case_id: str, call: str, reason: str, summary: str, identity: str
) -> str:
    """Promote a `transfer_to_team` request to its final outcome once a `"team-"`
    participant actually joins the room (see docs/escalation-handoff.md, "What happens
    on join"). Not called when no one answers -- that path goes to `escalate` instead."""
    case = store.get_case(case_id)
    if case is None:
        return UNKNOWN_CASE
    outcome = {
        "type": "handed_off",
        "reason": reason,
        "summary": summary,
        "joined_identity": identity,
        "joined_at": datetime.now(UTC).isoformat(),
    }
    if not store.set_outcome(case_id, outcome, status="handed off"):
        return NO_FINAL_OUTCOME
    return "Handed off."


def complete(store: CaseStore, case_id: str, call: str, summary: str) -> str:
    """Close the call, unless items are missing, in which case it escalates instead.

    Don't take the model's word that the case is done: a case with items still missing
    goes to a human, whatever the model decided.
    """
    case = store.get_case(case_id)
    if case is None:
        return UNKNOWN_CASE
    gaps = _missing_keys(case)
    if gaps:
        outcome = {
            "type": "escalated",
            "reason": "incomplete at close",
            "missing": gaps,
            "model_summary": summary,
        }
        if not store.set_outcome(case_id, outcome, status="escalated"):
            return NO_FINAL_OUTCOME
        return (
            "Some items are still missing; this call is being escalated instead. Say the "
            "review team will follow up on them, thank them, say goodbye, and stop talking."
        )
    outcome = {"type": "complete", "summary": summary}
    if not store.set_outcome(case_id, outcome, status="complete"):
        return NO_FINAL_OUTCOME
    return "Done. If you haven't already, thank them and say goodbye in one short sentence, then stop talking."


def load_case(store: CaseStore, case_id: str) -> dict | None:
    """The case shaped like `prompts.CASE`, so the prompt can be filled from data.

    See `outbound_ivr.tools.rendering` for prompt helpers that take this shape.
    """
    case = store.get_case(case_id)
    if case is None:
        return None
    office = store.get_office(case["office_id"]) if case.get("office_id") else None
    return {
        "payer": case.get("payer", ""),
        "caller_name": "Outbound IVR",
        "provider_office": (office or {}).get("name", case.get("provider_office", "")),
        "ordering_provider": case.get("ordering_provider", ""),
        "patient_name": case.get("patient_name", ""),
        "patient_dob": case.get("patient_dob", ""),
        "member_id": case.get("member_id", ""),
        "auth_ref": case.get("auth_ref", ""),
        "procedure": case.get("procedure", ""),
        "submitted_on": case.get("submitted_on", ""),
        "return_fax": case.get("return_fax", ""),
        "portal": case.get("portal", ""),
        "missing_items": [
            {
                "key": item["key"],
                "label": item.get("label", item["key"]),
                "ask": item.get("question", item.get("ask", "")),
            }
            for item in case.get("missing_items", [])
        ],
    }
