"""`GatewayBackend`, with a fake MCP client standing in for the AgentCore Gateway, and
`case_for_prompt()` against moto's mocked DynamoDB standing in for `outbound-ivr-cases`.

Also covers the SHARED CONTRACT this backend implements: SigV4 signing (via an
`httpx.Auth` hook, no `mcp-proxy-for-aws`), the `x-amzn-bedrock-agentcore-policy-
session-id` header staying stable for a whole phone call, the one retry on a 409
policy-session conflict, and that every tool call's name/arguments match
`lambdas/tools/schema.json` -- the schema the deployed Gateway target actually uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from moto import mock_aws

from outbound_ivr import intake
from outbound_ivr.tools.store import DynamoDBCaseStore, default_case
from outbound_ivr.voice import backend as backend_module
from outbound_ivr.voice.backend import (
    GATEWAY_POLICY_SESSION_HEADER,
    GATEWAY_TOOL_PREFIX,
    CaseNotFoundError,
    GatewayBackend,
    GatewayPolicySessionConflict,
    LocalBackend,
    make_backend,
)

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "src" / "lambdas" / "tools" / "schema.json").read_text()
)
REQUIRED_FIELDS = {tool["name"]: set(tool["inputSchema"]["required"]) for tool in SCHEMA["tools"]}
ALL_FIELDS = {
    tool["name"]: set(tool["inputSchema"]["properties"].keys()) for tool in SCHEMA["tools"]
}


def _assert_matches_schema(call: dict) -> None:
    """Every call `GatewayBackend` makes must be a tool the Gateway's own schema
    knows, with at least its required fields and nothing outside its known fields."""
    name = call["name"]
    assert name in REQUIRED_FIELDS, f"{name!r} is not one of the Gateway's tool schemas"
    arguments = call["arguments"] or {}
    missing = REQUIRED_FIELDS[name] - arguments.keys()
    assert not missing, f"{name} call is missing required fields {missing}"
    unknown = arguments.keys() - ALL_FIELDS[name]
    assert not unknown, f"{name} call has fields {unknown} the schema doesn't define"


class _FakeMCPClient:
    """Records every `call_tool_sync` call (and the session id it was built with); a
    no-op context manager, like `MCPClient`. Returns a canned success result unless
    `self._session_id` is in `rejected_session_ids`, in which case it returns the
    error-shaped result `strands.tools.mcp.MCPClient` produces when the policy-session
    conflict (409) exception surfaces from inside `call_tool_sync` -- see
    `GatewayPolicySessionConflict`'s docstring for why it's swallowed into text rather
    than raised.
    """

    def __init__(
        self, calls: list[dict], session_id: str, rejected_session_ids: frozenset[str] = frozenset()
    ) -> None:
        self._calls = calls
        self._session_id = session_id
        self._rejected = rejected_session_ids

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def call_tool_sync(self, tool_use_id, name, arguments=None, **kwargs):
        # The Gateway only knows the target-prefixed name; record the bare tool name.
        assert name.startswith(GATEWAY_TOOL_PREFIX), name
        name = name[len(GATEWAY_TOOL_PREFIX) :]
        self._calls.append({"name": name, "arguments": arguments, "session_id": self._session_id})
        if self._session_id in self._rejected:
            return {
                "status": "error",
                "content": [
                    {
                        "text": f"Tool execution failed: {backend_module._POLICY_SESSION_CONFLICT_MARKER}"
                    }
                ],
            }
        return {"status": "success", "content": [{"text": "ok"}]}


def _backend(
    rejected_session_ids: frozenset[str] = frozenset(),
) -> tuple[GatewayBackend, list[dict], dict]:
    calls: list[dict] = []
    record = intake.new_record()
    backend = GatewayBackend(
        record,
        mcp_client_factory=lambda session_id: _FakeMCPClient(
            calls, session_id, rejected_session_ids
        ),
    )
    return backend, calls, record


async def test_record_item_updates_local_record_and_calls_gateway_tool():
    backend, calls, record = _backend()
    key = intake.ITEM_KEYS[0]
    await backend.record_item(
        case_id="AUTH-1", call="call-1", key=key, value="Sept 22", confirmed=True
    )

    assert record["items"][key]["value"] == "Sept 22"
    assert record["items"][key]["confirmed"] is True
    assert len(calls) == 1
    assert calls[0]["name"] == "record_item"
    assert calls[0]["arguments"] == {
        "case_id": "AUTH-1",
        "call": "call-1",
        "key": key,
        "value": "Sept 22",
        "confirmed": True,
    }
    _assert_matches_schema(calls[0])


async def test_escalate_updates_local_record_and_calls_gateway_tool():
    backend, calls, record = _backend()
    outcome = await backend.escalate(
        case_id="AUTH-1", call="call-1", reason="refused", follow_up="call back Monday"
    )

    assert outcome["type"] == "escalated" and record["outcome"]["type"] == "escalated"
    assert calls[0]["name"] == "escalate"
    assert calls[0]["arguments"] == {
        "case_id": "AUTH-1",
        "call": "call-1",
        "reason": "refused",
        "follow_up": "call back Monday",
    }
    _assert_matches_schema(calls[0])


async def test_complete_updates_local_record_and_calls_gateway_tool():
    backend, calls, record = _backend()
    for key in intake.ITEM_KEYS:
        intake.record_item(record, key, "value", True)
    outcome = await backend.complete(case_id="AUTH-1", call="call-1", summary="all set")

    assert outcome["type"] == "complete"
    assert calls[0] == {
        "name": "complete",
        "arguments": {"case_id": "AUTH-1", "call": "call-1", "summary": "all set"},
        "session_id": "call-1",
    }
    _assert_matches_schema(calls[0])


async def test_transfer_to_team_updates_local_record_and_calls_gateway_tool():
    backend, calls, record = _backend()
    entry = await backend.transfer_to_team(
        case_id="AUTH-1",
        call="call-1",
        reason="needs a supervisor",
        summary="PT dates still missing",
    )

    assert entry["reason"] == "needs a supervisor"
    assert record["handoff_requests"] == [entry]
    assert calls[0]["name"] == "transfer_to_team"
    assert calls[0]["arguments"] == {
        "case_id": "AUTH-1",
        "call": "call-1",
        "reason": "needs a supervisor",
        "summary": "PT dates still missing",
    }
    _assert_matches_schema(calls[0])


async def test_mark_handoff_joined_does_not_call_the_gateway():
    """Not a model-called tool -- no corresponding Gateway tool/schema entry at all."""
    backend, calls, record = _backend()
    await backend.mark_handoff_joined(
        case_id="AUTH-1",
        call="call-1",
        reason="needs a supervisor",
        summary="summary",
        identity="team-1",
    )
    assert record["outcome"]["type"] == "handed_off"
    assert calls == []


async def test_complete_with_missing_items_still_escalates_like_local_backend():
    backend, calls, _record = _backend()
    outcome = await backend.complete(case_id="AUTH-1", call="call-1", summary="done anyway")

    assert outcome["type"] == "escalated"
    assert outcome["missing"] == intake.ITEM_KEYS
    assert calls[0]["name"] == "complete"


def test_make_backend_defaults_to_local(monkeypatch):
    monkeypatch.delenv("OUTBOUND_IVR_TOOLS", raising=False)
    backend = make_backend(intake.new_record())
    assert isinstance(backend, LocalBackend)


def test_make_backend_selects_gateway(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_TOOLS", "gateway")
    try:
        backend = make_backend(intake.new_record())
        assert isinstance(backend, GatewayBackend)
    finally:
        monkeypatch.delenv("OUTBOUND_IVR_TOOLS", raising=False)


# -- Policy-session header: stable per call, retried once on a 409 ------------------


async def test_policy_session_id_defaults_to_the_call_uuid_and_stays_stable():
    """Multiple tool calls in the same phone call (`call="call-1"` every time) must
    carry the same session id -- that's what lets `transfer_to_team_once.dw` tell
    "already called once this session" apart from a new session."""
    backend, calls, _record = _backend()
    await backend.record_item(
        case_id="AUTH-1", call="call-1", key=intake.ITEM_KEYS[0], value="x", confirmed=True
    )
    await backend.escalate(case_id="AUTH-1", call="call-1", reason="refused", follow_up=None)

    assert [c["session_id"] for c in calls] == ["call-1", "call-1"]


async def test_policy_session_conflict_retries_once_with_a_fresh_session_id():
    backend, calls, record = _backend(rejected_session_ids=frozenset({"call-1"}))
    key = intake.ITEM_KEYS[0]
    await backend.record_item(case_id="AUTH-1", call="call-1", key=key, value="x", confirmed=True)

    # The local record still updated -- the retry is purely about the Gateway side.
    assert record["items"][key]["value"] == "x"
    assert len(calls) == 2
    assert calls[0]["session_id"] == "call-1"
    assert calls[1]["session_id"] != "call-1"
    assert calls[1]["session_id"].startswith("call-1-")
    # Both calls still match the Gateway's schema -- the retry doesn't change what's sent.
    for call in calls:
        _assert_matches_schema(call)


async def test_policy_session_conflict_twice_in_a_row_gives_up_after_one_retry():
    """Only one retry, per the contract -- a second 409 (e.g. the engine is still
    mid-update) must surface rather than loop."""
    backend, calls, _record = _backend(rejected_session_ids=frozenset({"call-1"}))

    # Make the retry's fresh session id conflict too, by rejecting every session id
    # the fake client ever sees (a set-like object whose `in` check always says yes).
    class _RejectAll:
        def __contains__(self, _item):
            return True

    backend._mcp_client_factory = lambda session_id: _FakeMCPClient(calls, session_id, _RejectAll())

    with pytest.raises(GatewayPolicySessionConflict):
        await backend.record_item(
            case_id="AUTH-1", call="call-1", key=intake.ITEM_KEYS[0], value="x", confirmed=True
        )

    assert len(calls) == 2  # the original attempt, plus exactly one retry


def test_default_mcp_client_factory_signs_with_sigv4_and_sends_the_session_header(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_GATEWAY_URL", "https://gw.example.com/mcp")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    captured: dict = {}

    class _FakeMCPClientClass:
        def __init__(self, *, url, headers, auth_provider):
            captured["url"] = url
            captured["headers"] = headers
            captured["auth_provider"] = auth_provider

    monkeypatch.setattr("strands.tools.mcp.MCPClient", _FakeMCPClientClass)
    backend = GatewayBackend(intake.new_record())
    backend._default_mcp_client_factory("session-xyz")

    assert captured["url"] == "https://gw.example.com/mcp"
    assert captured["headers"] == {GATEWAY_POLICY_SESSION_HEADER: "session-xyz"}
    auth = captured["auth_provider"]
    assert isinstance(auth, backend_module._GatewaySigV4Auth)
    assert auth._service == "bedrock-agentcore"
    assert auth._region == "us-west-2"


class _FakeFrozenCredentials:
    access_key = "AKIATEST"
    secret_key = (
        "secret"  # pragma: allowlist secret  # nosec B105 -- fake signing key for a test double
    )
    token = None


class _FakeCredentials:
    def get_frozen_credentials(self):
        return _FakeFrozenCredentials()


class _FakeBotoSession:
    def __init__(self, credentials=None) -> None:
        self._credentials = credentials if credentials is not None else _FakeCredentials()

    def get_credentials(self):
        return self._credentials


def test_sigv4_auth_signs_the_request_without_needing_real_aws_credentials():
    auth = backend_module._GatewaySigV4Auth(
        service="bedrock-agentcore", region="us-east-1", session=_FakeBotoSession()
    )
    request = httpx.Request(
        "POST",
        "https://gw.example.com/mcp",
        headers={GATEWAY_POLICY_SESSION_HEADER: "call-1", "content-type": "application/json"},
        content=b'{"jsonrpc": "2.0"}',
    )

    flow = auth.auth_flow(request)
    signed = next(flow)

    assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert "X-Amz-Date" in signed.headers
    # The caller's own headers (the policy-session id) survive signing untouched.
    assert signed.headers[GATEWAY_POLICY_SESSION_HEADER] == "call-1"
    flow.close()


def test_sigv4_auth_raises_gateway_conflict_on_409():
    auth = backend_module._GatewaySigV4Auth(
        service="bedrock-agentcore", region="us-east-1", session=_FakeBotoSession()
    )
    request = httpx.Request("POST", "https://gw.example.com/mcp", content=b"{}")

    flow = auth.auth_flow(request)
    signed = next(flow)
    response = httpx.Response(409, request=signed)

    with pytest.raises(GatewayPolicySessionConflict):
        flow.send(response)


def test_sigv4_auth_passes_through_a_normal_response():
    auth = backend_module._GatewaySigV4Auth(
        service="bedrock-agentcore", region="us-east-1", session=_FakeBotoSession()
    )
    request = httpx.Request("POST", "https://gw.example.com/mcp", content=b"{}")

    flow = auth.auth_flow(request)
    signed = next(flow)
    response = httpx.Response(200, request=signed)

    with pytest.raises(StopIteration):
        flow.send(response)


def test_sigv4_auth_raises_without_credentials():
    class _NoCredentialsSession:
        def get_credentials(self):
            return None

    auth = backend_module._GatewaySigV4Auth(
        service="bedrock-agentcore", region="us-east-1", session=_NoCredentialsSession()
    )
    request = httpx.Request("POST", "https://gw.example.com/mcp", content=b"{}")

    with pytest.raises(RuntimeError):
        next(auth.auth_flow(request))


# -- case_for_prompt (unchanged by the auth swap) ------------------------------------

CASES_TABLE = "outbound-ivr-cases-test"
OFFICES_TABLE = "outbound-ivr-offices-test"


def test_case_for_prompt_without_a_table_falls_back_to_none(monkeypatch):
    """No `OUTBOUND_IVR_CASES_TABLE`: today's behaviour, the caller uses the fixed demo case."""
    monkeypatch.delenv("OUTBOUND_IVR_CASES_TABLE", raising=False)
    backend = GatewayBackend(
        intake.new_record(),
        "AUTH-1",
        mcp_client_factory=lambda session_id: _FakeMCPClient([], session_id),
    )
    assert backend.case_for_prompt() is None


def test_case_for_prompt_reads_the_real_case_from_dynamodb(monkeypatch):
    """`OUTBOUND_IVR_CASES_TABLE` set: the real case comes back, shaped for rendering."""
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", CASES_TABLE)
    monkeypatch.setenv("OUTBOUND_IVR_OFFICES_TABLE", OFFICES_TABLE)
    with mock_aws():
        DynamoDBCaseStore.ensure_tables(CASES_TABLE, OFFICES_TABLE, region_name="us-east-1")
        store = DynamoDBCaseStore(
            cases_table=CASES_TABLE, offices_table=OFFICES_TABLE, region_name="us-east-1"
        )
        store.put_case(
            default_case(
                "AUTH-REAL",
                payer="Real Health Plan",
                provider_office="Real Medical Group",
                patient_name="Pat Real",
                auth_ref="AUTH-REAL",
                missing_items=[
                    {"key": "progress_note", "label": "Progress note", "question": "the note"}
                ],
            )
        )

        backend = GatewayBackend(
            intake.new_record(),
            "AUTH-REAL",
            mcp_client_factory=lambda session_id: _FakeMCPClient([], session_id),
            case_store_factory=lambda: DynamoDBCaseStore(
                cases_table=CASES_TABLE, offices_table=OFFICES_TABLE, region_name="us-east-1"
            ),
        )
        case = backend.case_for_prompt()

    assert case is not None
    assert case["payer"] == "Real Health Plan"
    assert case["provider_office"] == "Real Medical Group"
    assert case["missing_items"] == [
        {"key": "progress_note", "label": "Progress note", "ask": "the note"}
    ]


def test_case_for_prompt_raises_rather_than_fall_back_when_case_is_missing(monkeypatch):
    """A real store is configured but the case_id isn't in it: never use the demo case."""
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", CASES_TABLE)
    monkeypatch.setenv("OUTBOUND_IVR_OFFICES_TABLE", OFFICES_TABLE)
    with mock_aws():
        DynamoDBCaseStore.ensure_tables(CASES_TABLE, OFFICES_TABLE, region_name="us-east-1")

        backend = GatewayBackend(
            intake.new_record(),
            "AUTH-MISSING",
            mcp_client_factory=lambda session_id: _FakeMCPClient([], session_id),
            case_store_factory=lambda: DynamoDBCaseStore(
                cases_table=CASES_TABLE, offices_table=OFFICES_TABLE, region_name="us-east-1"
            ),
        )
        with pytest.raises(CaseNotFoundError) as exc_info:
            backend.case_for_prompt()

    assert exc_info.value.case_id == "AUTH-MISSING"


def test_gateway_requests_are_signed_for_the_gateways_own_region(monkeypatch) -> None:
    """A shell with AWS_REGION=us-west-2 signed for us-west-2 and got InvalidSignature."""
    from outbound_ivr.voice.backend import gateway_region

    monkeypatch.setenv("AWS_REGION", "us-west-2")
    url = "https://outbound-ivr-tools-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    assert gateway_region(url) == "us-east-1"
    assert gateway_region("http://localhost:8000/mcp") == "us-west-2"


# --------------------------------------------------------------------------
# One MCP session per phone call, not one per tool call. A fresh client meant a
# fresh TLS handshake, SigV4 signature and MCP initialize on every tool call,
# mid-conversation, with the caller listening to the gap.
# --------------------------------------------------------------------------


class _CountingMCPClient(_FakeMCPClient):
    """Counts enters and exits so the test can see sessions, not just calls."""

    def __init__(self, calls, session_id, rejected_session_ids=frozenset(), ledger=None):
        super().__init__(calls, session_id, rejected_session_ids)
        self._ledger = ledger if ledger is not None else {"entered": 0, "exited": 0}

    def __enter__(self):
        self._ledger["entered"] += 1
        return self

    def __exit__(self, *exc_info):
        self._ledger["exited"] += 1
        return False


def _counting_backend(rejected_session_ids: frozenset[str] = frozenset()):
    calls: list[dict] = []
    ledger = {"entered": 0, "exited": 0}
    backend = GatewayBackend(
        intake.new_record(),
        mcp_client_factory=lambda session_id: _CountingMCPClient(
            calls, session_id, rejected_session_ids, ledger
        ),
    )
    return backend, calls, ledger


@pytest.mark.asyncio
async def test_four_tool_calls_share_one_mcp_session():
    backend, calls, ledger = _counting_backend()
    for key in ("mri_date", "pt_duration", "conservative_care"):
        await backend.record_item(
            case_id="AUTH-1", call="call-1", key=key, value="x", confirmed=True
        )
    await backend.complete(case_id="AUTH-1", call="call-1", summary="done")

    assert len(calls) == 4
    assert ledger["entered"] == 1, "one session for the whole call"
    assert ledger["exited"] == 0, "and it stays open until the call ends"


@pytest.mark.asyncio
async def test_aclose_releases_the_session_and_is_idempotent():
    backend, _calls, ledger = _counting_backend()
    await backend.record_item(
        case_id="AUTH-1", call="call-1", key="mri_date", value="x", confirmed=True
    )
    await backend.aclose()
    assert ledger["exited"] == 1
    await backend.aclose()
    assert ledger["exited"] == 1, "closing twice must not raise or double-exit"


@pytest.mark.asyncio
async def test_a_policy_session_conflict_opens_a_new_session_for_the_retry():
    """The policy-session header is baked in when the client is built, so the
    409 retry cannot reuse the rejected session's client."""
    backend, calls, ledger = _counting_backend(rejected_session_ids=frozenset({"call-1"}))
    await backend.record_item(
        case_id="AUTH-1", call="call-1", key="mri_date", value="x", confirmed=True
    )
    assert ledger["entered"] == 2, "rejected session closed, a fresh one opened"
    assert calls[0]["session_id"] == "call-1"
    assert calls[1]["session_id"] != "call-1"


@pytest.mark.asyncio
async def test_a_raising_client_is_not_reused():
    calls: list[dict] = []
    ledger = {"entered": 0, "exited": 0}

    class _Boom(_CountingMCPClient):
        def call_tool_sync(self, *a, **k):
            raise RuntimeError("transport died")

    backend = GatewayBackend(
        intake.new_record(),
        mcp_client_factory=lambda session_id: _Boom(calls, session_id, frozenset(), ledger),
    )
    with pytest.raises(RuntimeError):
        await backend.record_item(
            case_id="AUTH-1", call="call-1", key="mri_date", value="x", confirmed=True
        )
    assert ledger["exited"] == 1, "a session that blew up must not serve the next call"


# --------------------------------------------------------------------------
# Non-409 failures. MCPClient.call_tool_sync swallows a dropped connection or a
# timeout into an ordinary {"status": "error"} return rather than raising, so
# `except` never sees them. The cached client used to stay in place and the
# tool wrappers never looked at the result, which meant one mid-call hiccup
# silently dropped every item for the rest of the call while Outbound IVR kept telling
# the caller each one was saved.
# --------------------------------------------------------------------------


class _FailingThenOkClient(_CountingMCPClient):
    """Returns a non-409 error result on the first N calls, success afterwards."""

    failures: ClassVar[dict] = {}

    def call_tool_sync(self, tool_use_id, name, arguments=None, **kwargs):
        bare = name[len(GATEWAY_TOOL_PREFIX) :]
        self._calls.append({"name": bare, "arguments": arguments, "session_id": self._session_id})
        remaining = _FailingThenOkClient.failures.get("n", 0)
        if remaining:
            _FailingThenOkClient.failures["n"] = remaining - 1
            return {"status": "error", "content": [{"text": "connection closed by peer"}]}
        return {"status": "success", "content": [{"text": "ok"}]}


def _flaky_backend(failures: int):
    calls: list[dict] = []
    ledger = {"entered": 0, "exited": 0}
    _FailingThenOkClient.failures = {"n": failures}
    backend = GatewayBackend(
        intake.new_record(),
        mcp_client_factory=lambda session_id: _FailingThenOkClient(
            calls, session_id, frozenset(), ledger
        ),
    )
    return backend, calls, ledger


@pytest.mark.asyncio
async def test_a_transport_failure_is_retried_on_a_fresh_session():
    backend, calls, ledger = _flaky_backend(failures=1)
    await backend.record_item(
        case_id="AUTH-1", call="call-1", key="mri_date", value="x", confirmed=True
    )
    assert len(calls) == 2, "the failed call must be retried"
    assert ledger["exited"] == 1, "the broken session must be closed, not reused"
    assert ledger["entered"] == 2
    # The policy session id must NOT rotate: rotating it would reset the temporal
    # policy's count and hand the call a second warm transfer.
    assert calls[0]["session_id"] == calls[1]["session_id"] == "call-1"


@pytest.mark.asyncio
async def test_a_failure_that_survives_the_retry_reaches_the_caller():
    """It used to be a WARNING and a returned value nobody inspected, so the tool
    told the model the item was saved when the Gateway had refused."""
    from outbound_ivr.voice.backend import GatewayToolCallFailed

    backend, calls, _ledger = _flaky_backend(failures=2)
    with pytest.raises(GatewayToolCallFailed) as raised:
        await backend.record_item(
            case_id="AUTH-1", call="call-1", key="mri_date", value="x", confirmed=True
        )
    assert "connection closed by peer" in str(raised.value)
    assert len(calls) == 2, "one retry, then give up"


@pytest.mark.asyncio
async def test_every_gateway_tool_propagates_a_surviving_failure():
    from outbound_ivr.voice.backend import GatewayToolCallFailed

    for coro_name, kwargs in [
        ("record_item", {"key": "mri_date", "value": "x", "confirmed": True}),
        ("escalate", {"reason": "front desk refused", "follow_up": ""}),
        ("complete", {"summary": "done"}),
        ("transfer_to_team", {"reason": "needs a person", "summary": "s"}),
    ]:
        backend, _calls, _ledger = _flaky_backend(failures=99)
        with pytest.raises(GatewayToolCallFailed):
            await getattr(backend, coro_name)(case_id="AUTH-1", call="call-1", **kwargs)


@pytest.mark.asyncio
async def test_a_409_during_the_mcp_handshake_is_still_retried():
    """MCPClient.start() wraps anything that fails during initialize in
    MCPClientInitializationError, so the 409 used to reach _to_thread as the wrong
    type and was never retried."""
    from outbound_ivr.voice.backend import GatewayPolicySessionConflict

    state = {"first": True}
    calls: list[dict] = []

    class _ConflictOnFirstEnter(_FakeMCPClient):
        def __enter__(self):
            if state["first"]:
                state["first"] = False
                raise RuntimeError("MCP client initialization failed") from (
                    GatewayPolicySessionConflict(self._session_id)
                )
            return self

    backend = GatewayBackend(
        intake.new_record(),
        mcp_client_factory=lambda session_id: _ConflictOnFirstEnter(calls, session_id),
    )
    await backend.record_item(
        case_id="AUTH-1", call="call-1", key="mri_date", value="x", confirmed=True
    )
    assert len(calls) == 1
    assert calls[0]["session_id"] != "call-1", "the retry needs a fresh policy session"
