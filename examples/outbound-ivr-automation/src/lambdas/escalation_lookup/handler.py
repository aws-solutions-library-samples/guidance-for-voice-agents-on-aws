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

A code is good for one contact, once, until its `expires_at` (written with the code,
15 minutes out). DynamoDB's own TTL deletion runs on its own schedule, often well
after that, so the expiry is checked here rather than left to TTL. The lookup and the
"used" mark are one conditional `UpdateItem`: the first contact to present a code
claims it, and any other contact gets "not recognized". The same contact may look the
code up again, so a Connect retry of this invocation (throttling, a timeout after the
claim already landed) still finds it.
"""

from __future__ import annotations

import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = os.environ["HANDOFF_TABLE"]
_table = boto3.resource("dynamodb").Table(TABLE_NAME)


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, str]:
    contact = event.get("Details", {}).get("ContactData", {})
    attributes = contact.get("Attributes", {})
    code = (attributes.get("handoff_code") or "").strip()
    if not code:
        return {"found": "false", "summary": "No handoff code was captured."}

    contact_id = (contact.get("ContactId") or "").strip()
    item = _claim_code(code, contact_id) if contact_id else None
    if item is None:
        return {"found": "false", "summary": f"Handoff code {code} was not recognized."}

    return {
        "found": "true",
        "case_id": str(item.get("case_id", "")),
        "items_collected": str(item.get("items_collected", "")),
        "escalation_reason": str(item.get("escalation_reason", "")),
        "summary": str(item.get("summary", "")),
    }


def _claim_code(code: str, contact_id: str) -> dict[str, Any] | None:
    """The code's item, marked used by `contact_id`, or None if the code does not
    exist, has expired, or was already used by a different contact. Any other
    DynamoDB error propagates, so the flow takes its lookup-error branch."""
    try:
        return _table.update_item(
            Key={"code": code},
            UpdateExpression="SET used_by = :contact",
            ConditionExpression=(
                "attribute_exists(#code) AND expires_at > :now"
                " AND (attribute_not_exists(used_by) OR used_by = :contact)"
            ),
            ExpressionAttributeNames={"#code": "code"},
            ExpressionAttributeValues={":contact": contact_id, ":now": int(time.time())},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
