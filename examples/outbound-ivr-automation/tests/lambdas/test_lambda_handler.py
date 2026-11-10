"""lambdas/tools/handler.py: the Gateway Lambda target, simulated events and context.

Event format per lambdas/README.md: the tool's input fields are the event itself (no
envelope); the tool name lives on the Lambda context, prefixed with the Gateway
target's name, e.g. "OutboundIvrTools___record_item".
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from moto import mock_aws

from outbound_ivr.tools.store import DynamoDBCaseStore, default_case

LAMBDAS_TOOLS = Path(__file__).resolve().parents[2] / "src" / "lambdas" / "tools"


def _load_handler_module():
    spec = importlib.util.spec_from_file_location(
        "outbound_ivr_lambda_tools_handler", LAMBDAS_TOOLS / "handler.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handler = _load_handler_module()


class FakeClientContext:
    def __init__(self, tool_name: str):
        self.custom = {"bedrockAgentCoreToolName": tool_name}


class FakeContext:
    def __init__(self, tool_name: str):
        self.client_context = FakeClientContext(tool_name)


@pytest.fixture(autouse=True)
def _reset_module_store(monkeypatch):
    # The handler module caches a store at module scope; make every test build a fresh
    # one against its own moto-mocked DynamoDB.
    monkeypatch.setattr(handler, "_store", None)
    yield
    monkeypatch.setattr(handler, "_store", None)


@pytest.fixture
def dynamodb(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", "outbound-ivr-cases")
    monkeypatch.setenv("OUTBOUND_IVR_OFFICES_TABLE", "outbound-ivr-offices")
    monkeypatch.delenv("OUTBOUND_IVR_DYNAMODB_ENDPOINT", raising=False)
    with mock_aws():
        DynamoDBCaseStore.ensure_tables(
            "outbound-ivr-cases", "outbound-ivr-offices", region_name="us-east-1"
        )
        store = DynamoDBCaseStore(
            cases_table="outbound-ivr-cases",
            offices_table="outbound-ivr-offices",
            region_name="us-east-1",
        )
        store.put_case(
            default_case(
                "case-1",
                missing_items=[{"key": "physical_therapy", "label": "PT", "question": "dates"}],
            )
        )
        yield store


def test_tool_name_from_context_strips_the_gateway_prefix():
    assert (
        handler.tool_name_from_context(FakeContext("OutboundIvrTools___record_item"))
        == "record_item"
    )


def test_tool_name_from_context_without_a_prefix():
    assert handler.tool_name_from_context(FakeContext("record_item")) == "record_item"


def test_tool_name_from_context_missing_custom():
    class EmptyContext:
        client_context = None

    assert handler.tool_name_from_context(EmptyContext()) == ""


def test_record_item_dispatch(dynamodb):
    event = {
        "case_id": "case-1",
        "call": "call-1",
        "key": "physical_therapy",
        "value": "6 weeks",
        "confirmed": True,
    }
    result = handler.lambda_handler(event, FakeContext("OutboundIvrTools___record_item"))
    assert result == {"result": "Saved."}
    assert dynamodb.get_case("case-1")["results"]["physical_therapy"]["value"] == "6 weeks"


def test_escalate_dispatch(dynamodb):
    event = {"case_id": "case-1", "call": "call-1", "reason": "no one available"}
    result = handler.lambda_handler(event, FakeContext("OutboundIvrTools___escalate"))
    assert result["result"].startswith("Escalated.")
    assert dynamodb.get_case("case-1")["status"] == "escalated"


def test_complete_dispatch_escalates_when_items_missing(dynamodb):
    event = {"case_id": "case-1", "call": "call-1", "summary": "all done"}
    result = handler.lambda_handler(event, FakeContext("OutboundIvrTools___complete"))
    assert "being escalated instead" in result["result"]


def test_transfer_to_team_dispatch(dynamodb):
    event = {
        "case_id": "case-1",
        "call": "call-1",
        "reason": "front desk wants a supervisor",
        "summary": "needs PT dates confirmed",
    }
    result = handler.lambda_handler(event, FakeContext("OutboundIvrTools___transfer_to_team"))
    assert result == {"result": "Requested."}
    case = dynamodb.get_case("case-1")
    assert case["status"] == "handoff_requested"
    assert case["outcome"]["type"] == "transfer_to_team"


def test_transfer_to_team_requires_a_reason(dynamodb):
    event = {"case_id": "case-1", "call": "call-1", "reason": "", "summary": "x"}
    result = handler.lambda_handler(event, FakeContext("OutboundIvrTools___transfer_to_team"))
    assert "reason is required" in result["result"]


def test_unknown_tool_returns_error(dynamodb):
    result = handler.lambda_handler({}, FakeContext("OutboundIvrTools___delete_everything"))
    assert "error" in result
    assert "delete_everything" in result["error"]


def test_missing_field_returns_error(dynamodb):
    result = handler.lambda_handler(
        {"case_id": "case-1"}, FakeContext("OutboundIvrTools___record_item")
    )
    assert "error" in result
    assert "Missing required field" in result["error"]
