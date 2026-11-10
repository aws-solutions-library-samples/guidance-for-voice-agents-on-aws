"""DynamoDBCaseStore against moto's mocked DynamoDB -- same contract as
InMemoryCaseStore (test_tools_business.py exercises the rules; this exercises the
DynamoDB-specific plumbing: table creation, conditional writes, Decimal round-tripping).
"""

import pytest
from moto import mock_aws

from outbound_ivr.tools.store import DynamoDBCaseStore, default_case

CASES_TABLE = "outbound-ivr-cases-test"
OFFICES_TABLE = "outbound-ivr-offices-test"


@pytest.fixture
def store():
    with mock_aws():
        DynamoDBCaseStore.ensure_tables(CASES_TABLE, OFFICES_TABLE, region_name="us-east-1")
        yield DynamoDBCaseStore(
            cases_table=CASES_TABLE, offices_table=OFFICES_TABLE, region_name="us-east-1"
        )


def test_ensure_tables_is_idempotent():
    with mock_aws():
        first = DynamoDBCaseStore.ensure_tables(CASES_TABLE, OFFICES_TABLE, region_name="us-east-1")
        assert first == {CASES_TABLE: True, OFFICES_TABLE: True}
        second = DynamoDBCaseStore.ensure_tables(
            CASES_TABLE, OFFICES_TABLE, region_name="us-east-1"
        )
        assert second == {CASES_TABLE: False, OFFICES_TABLE: False}


def test_put_and_get_case_round_trips(store):
    store.put_case(default_case("case-1", payer="Example Health Plan", attempts=0))
    case = store.get_case("case-1")
    assert case["case_id"] == "case-1"
    assert case["payer"] == "Example Health Plan"
    assert isinstance(case["attempts"], int)
    assert case["attempts"] == 0


def test_get_unknown_case_returns_none(store):
    assert store.get_case("nope") is None


def test_put_and_get_office(store):
    store.put_office(
        {"office_id": "office-1", "name": "AnyCompany Medical Group", "number": "+15555550100"}
    )
    office = store.get_office("office-1")
    assert office["name"] == "AnyCompany Medical Group"


def test_record_result_then_get(store):
    store.put_case(
        default_case("case-1", missing_items=[{"key": "k", "label": "K", "question": "ask"}])
    )
    updated = store.record_result("case-1", "call-1", "k", "an answer", True)
    assert updated["results"]["k"]["value"] == "an answer"
    assert updated["results"]["k"]["confirmed"] is True

    case = store.get_case("case-1")
    assert case["results"]["k"]["call"] == "call-1"


def test_record_result_unknown_case_returns_none(store):
    assert store.record_result("nope", "call-1", "k", "v", True) is None


def test_increment_attempts(store):
    store.put_case(default_case("case-1"))
    assert store.increment_attempts("case-1") == 1
    assert store.increment_attempts("case-1") == 2
    assert store.get_case("case-1")["attempts"] == 2


def test_increment_attempts_unknown_case_returns_none(store):
    assert store.increment_attempts("nope") is None


def test_set_outcome_then_blocked(store):
    store.put_case(default_case("case-1"))
    assert (
        store.set_outcome("case-1", {"type": "complete", "summary": "done"}, status="complete")
        is True
    )
    case = store.get_case("case-1")
    assert case["status"] == "complete"
    assert case["outcome"]["summary"] == "done"

    # A final outcome is not overwritten.
    assert (
        store.set_outcome("case-1", {"type": "escalated", "reason": "late"}, status="escalated")
        is False
    )
    assert store.get_case("case-1")["outcome"]["summary"] == "done"


def test_set_outcome_unknown_case_returns_false(store):
    assert (
        store.set_outcome("nope", {"type": "complete", "summary": "x"}, status="complete") is False
    )


def test_set_status(store):
    store.put_case(default_case("case-1"))
    store.set_status("case-1", "calling")
    assert store.get_case("case-1")["status"] == "calling"
