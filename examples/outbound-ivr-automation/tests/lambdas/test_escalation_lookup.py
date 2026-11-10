"""lambdas/escalation_lookup/handler.py: the Evidence team flow's handoff code lookup.

A code is good for one contact, once, until its `expires_at`; the same contact may
look it up again (a Connect retry of the invocation).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

HANDLER = (
    Path(__file__).resolve().parents[2] / "src" / "lambdas" / "escalation_lookup" / "handler.py"
)
TABLE = "outbound-ivr-handoff-codes"


@pytest.fixture
def lookup(monkeypatch):
    """The handler module, loaded inside a moto mock with an empty handoff table (it
    builds its boto3 Table at import time, so the mock must already be active)."""
    monkeypatch.setenv("HANDOFF_TABLE", TABLE)
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "code", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "code", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        spec = importlib.util.spec_from_file_location("outbound_ivr_escalation_lookup", HANDLER)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        yield module
        sys.modules.pop(spec.name, None)


def _put_code(code: str = "123456", *, expires_in: int = 900, **extra) -> None:
    item = {
        "code": code,
        "case_id": "case-1",
        "items_collected": "visit notes",
        "escalation_reason": "office asked for a person",
        "summary": "Needs the visit notes from March.",
        "expires_at": int(time.time()) + expires_in,
        **extra,
    }
    boto3.resource("dynamodb").Table(TABLE).put_item(Item=item)


def _event(code: str | None = "123456", contact_id: str | None = "contact-1") -> dict:
    contact: dict = {"Attributes": {"handoff_code": code} if code is not None else {}}
    if contact_id is not None:
        contact["ContactId"] = contact_id
    return {"Details": {"ContactData": contact}}


def test_a_live_code_is_found_and_returns_the_case_fields(lookup) -> None:
    _put_code()
    assert lookup.lambda_handler(_event(), None) == {
        "found": "true",
        "case_id": "case-1",
        "items_collected": "visit notes",
        "escalation_reason": "office asked for a person",
        "summary": "Needs the visit notes from March.",
    }


def test_a_missing_code_is_not_found(lookup) -> None:
    assert lookup.lambda_handler(_event(code=None), None)["found"] == "false"
    assert lookup.lambda_handler(_event(code="  "), None)["found"] == "false"


def test_an_unknown_code_is_not_found_and_creates_no_item(lookup) -> None:
    result = lookup.lambda_handler(_event(code="999999"), None)
    assert result["found"] == "false"
    assert "Item" not in boto3.resource("dynamodb").Table(TABLE).get_item(Key={"code": "999999"})


def test_an_expired_code_is_not_found_even_before_ttl_deletes_it(lookup) -> None:
    _put_code(expires_in=-1)
    assert lookup.lambda_handler(_event(), None)["found"] == "false"


def test_a_code_expiring_this_second_is_not_found(lookup) -> None:
    _put_code(expires_in=0)
    assert lookup.lambda_handler(_event(), None)["found"] == "false"


def test_a_code_works_once_and_then_not_for_another_contact(lookup) -> None:
    _put_code()
    assert lookup.lambda_handler(_event(contact_id="contact-1"), None)["found"] == "true"
    assert lookup.lambda_handler(_event(contact_id="contact-2"), None)["found"] == "false"


def test_the_same_contact_can_look_the_code_up_again(lookup) -> None:
    """A retried invocation for the contact that claimed the code still gets it."""
    _put_code()
    first = lookup.lambda_handler(_event(contact_id="contact-1"), None)
    retry = lookup.lambda_handler(_event(contact_id="contact-1"), None)
    assert first == retry
    assert retry["found"] == "true"


def test_a_claimed_code_records_the_contact(lookup) -> None:
    _put_code()
    lookup.lambda_handler(_event(contact_id="contact-1"), None)
    item = boto3.resource("dynamodb").Table(TABLE).get_item(Key={"code": "123456"})["Item"]
    assert item["used_by"] == "contact-1"


def test_an_invocation_without_a_contact_id_is_not_found(lookup) -> None:
    _put_code()
    assert lookup.lambda_handler(_event(contact_id=None), None)["found"] == "false"
    # ...and did not use up the code.
    assert lookup.lambda_handler(_event(contact_id="contact-1"), None)["found"] == "true"


def test_a_dynamodb_error_other_than_the_condition_propagates(lookup, monkeypatch) -> None:
    """Raising lets the flow take its existing lookup-error branch instead of
    reporting a real code as not recognized."""

    def fail(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
            "UpdateItem",
        )

    monkeypatch.setattr(lookup._table, "update_item", fail)
    with pytest.raises(ClientError):
        lookup.lambda_handler(_event(), None)
