"""outbound-ivr escalation handoff lookup.

Invoked synchronously from the Evidence team inbound flow's "Invoke AWS Lambda
function" block (see infrastructure/stacks/escalation_stack.py and
docs/escalation-handoff.md), right after that flow's "Store customer input" block
captures a 6-digit handoff code and an "Update contact attributes" block copies it
into the `handoff_code` contact attribute (so it shows up here as a plain
`Details.ContactData.Attributes` value, not something this handler has to parse out
of flow-specific plumbing).

Looks the code up in the handoff-codes DynamoDB table (one item per code: case_id,
items_collected, escalation_reason, summary -- written by
`outbound_ivr.voice.backend.AwsTeamDialer` right before the evidence-team leg is
dialed) and returns a flat string map, the shape Connect requires
when the flow's Lambda block sets `Response validation` to `STRING_MAP`. The flow
copies these straight into contact attributes (`$.External.*`) with its own
"Update contact attributes" block right after this call -- this handler does not
call back into Connect itself.
"""

from __future__ import annotations

import os
from typing import Any

import boto3

TABLE_NAME = os.environ["HANDOFF_TABLE"]
_table = boto3.resource("dynamodb").Table(TABLE_NAME)


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, str]:
    attributes = event.get("Details", {}).get("ContactData", {}).get("Attributes", {})
    code = (attributes.get("handoff_code") or "").strip()
    if not code:
        return {"found": "false", "summary": "No handoff code was captured."}

    item = _table.get_item(Key={"code": code}).get("Item")
    if item is None:
        return {"found": "false", "summary": f"Handoff code {code} was not recognized."}

    return {
        "found": "true",
        "case_id": str(item.get("case_id", "")),
        "items_collected": str(item.get("items_collected", "")),
        "escalation_reason": str(item.get("escalation_reason", "")),
        "summary": str(item.get("summary", "")),
    }
