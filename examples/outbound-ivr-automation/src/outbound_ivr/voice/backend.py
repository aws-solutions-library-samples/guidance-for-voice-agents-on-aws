"""Tool binding for the intake tools (`record_item`, `escalate`, `complete`).

The model-facing Strands tools in `agent.py` keep their exact names, docstrings, and
arguments; this module is what they do with those arguments underneath, selected by
`OUTBOUND_IVR_TOOLS` (`local` default, or `gateway`), per docs/design.md's "Tools":

- `LocalBackend` applies `outbound_ivr.intake`'s rules to the call's in-memory record -- that
  copy is what `agent.py`'s case record and goodbye/hang-up logic still read -- and, in
  parallel, calls the same `outbound_ivr.tools.business` functions the Lambda handlers behind
  the Gateway use, against an `InMemoryCaseStore` seeded from `prompts.CASE`. So
  `OUTBOUND_IVR_TOOLS=local` exercises the shared business-rule code (validation, "complete
  with items missing becomes escalated") that backs every tool binding, not a second
  copy of those rules; `outbound_ivr.intake` stays as the call's own bookkeeping because the
  rest of `agent.py` (closing logic, the written case record) is built around its shape.
- `GatewayBackend` calls the AgentCore Gateway's MCP tools (`record_item`/`escalate`/
  `complete`/`transfer_to_team`) over streamable HTTP, through Strands' `MCPClient`.
  The Gateway's inbound authorizer is `AWS_IAM` (SigV4), not Cognito: Outbound IVR signs every
  request itself with botocore's `SigV4Auth`, using the AgentCore Runtime's own
  execution-role credentials (the default boto3 session), via an `httpx.Auth` hook
  passed to `MCPClient(auth_provider=...)` -- no token endpoint, no NAT gateway, the
  Runtime's ambient credentials are enough. Every call also carries the Gateway's own
  `x-amzn-bedrock-agentcore-policy-session-id` header, stable for the whole phone call
  (the policy engine's temporal rules, e.g. "transfer_to_team once per session", key
  off it); a 409 (`ConflictException`, raised when a policy update invalidates every
  open session) gets exactly one retry with a fresh session id. It still applies
  `outbound_ivr.intake`'s rules to the local record (via `LocalBackend`) for the same reason
  as `OUTBOUND_IVR_TOOLS=local` above.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import re
import secrets
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import httpx
import structlog
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from outbound_ivr import intake, prompts
from outbound_ivr.tools import business
from outbound_ivr.tools.store import CaseStore, InMemoryCaseStore, case_store_from_env, default_case

logger = structlog.get_logger("outbound_ivr.voice.backend")


class CaseNotFoundError(RuntimeError):
    """`case_for_prompt()` found a real case store configured, but no case with this
    case_id in it. Callers must end the call rather than fall back to the demo case."""

    def __init__(self, case_id: str) -> None:
        super().__init__(f"case {case_id!r} not found")
        self.case_id = case_id


@dataclasses.dataclass
class HandoffResult:
    """What a `TeamDialer` found out: did a human join, and if so, under what identity.

    `joined=False` is an expected outcome (nobody answered within the wait), not an
    error -- see `TeamDialer.request_handoff`.

    `briefing`, when set, overrides the model's own `summary` as the sentence Outbound IVR
    speaks to introduce the colleague (`agent.py`'s `_warm_introduction_instruction`):
    `AwsTeamDialer`'s direct mode sets it to a sentence it composes itself from `reason`
    and `items` rather than trust the model's freeform `summary`, which is otherwise the
    only thing said aloud to whoever answers -- see `AwsTeamDialer._request_handoff_direct`.
    `None` (every other dialer) keeps today's behavior: the model's own `summary` is
    what gets said.
    """

    joined: bool
    identity: str | None = None
    briefing: str | None = None


class TeamDialer(ABC):
    """How a human is actually reached for `transfer_to_team`, LiveKit mode only.

    Parallel to `IntakeBackend`, not a subclass of it: this answers "how do we get a
    person onto this call", a different axis from
    `IntakeBackend` ("what to record") or `Transport` ("what actually happens on the
    call"). See docs/escalation-handoff.md, "TeamDialer: how the human is reached", for
    the local (dashboard-rings-a-card) and AWS (spec-only) implementations; neither
    lives in this module.
    """

    @abstractmethod
    async def request_handoff(
        self, *, room: str, summary: str, reason: str, items: dict[str, Any]
    ) -> HandoffResult:
        """Ask for a human on `room`, and wait for one to join (or for the wait to run
        out). `items` is `state.record["items"]` as it stands right now."""

    async def clear_handoff(self, room: str) -> None:
        """Best-effort: tell whoever was asked for a human on `room` that it no longer
        matters -- the call ended before anyone joined (or has ended since). A no-op
        unless overridden: only a dialer that rang something outside the call itself
        needs this, so that it doesn't keep showing "ringing"/"connected" after the
        call it belonged to is long gone."""
        return


class IntakeBackend(ABC):
    """What `record_item`/`escalate`/`complete`/`transfer_to_team` call underneath."""

    @abstractmethod
    async def record_item(
        self, *, case_id: str | None, call: str, key: str, value: str, confirmed: bool
    ) -> None: ...

    @abstractmethod
    async def escalate(
        self, *, case_id: str | None, call: str, reason: str, follow_up: str | None
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def complete(self, *, case_id: str | None, call: str, summary: str) -> dict[str, Any]: ...

    @abstractmethod
    async def transfer_to_team(
        self, *, case_id: str | None, call: str, reason: str, summary: str
    ) -> dict[str, Any]:
        """Record a warm-handoff request (LiveKit mode only, docs/escalation-handoff.md).

        This records the request, with status `"handoff_requested"`: a provisional,
        non-final outcome -- the call isn't over yet, so this must not block a later
        `complete`/`escalate` on the same case the way a final outcome would.
        """

    @abstractmethod
    async def mark_handoff_joined(
        self, *, case_id: str | None, call: str, reason: str, summary: str, identity: str
    ) -> dict[str, Any]:
        """Promote a `transfer_to_team` request to its final outcome once a `"team-"`
        participant actually joins the room (docs/escalation-handoff.md, "What happens
        on join"). Called by `transfer_to_team`'s own tool, not the model."""

    def case_for_prompt(self) -> dict[str, Any] | None:
        """The case, shaped for `outbound_ivr.tools.rendering`, to fill a case-id-driven prompt.

        `None` means "use the fixed `prompts.CASE`" -- the default when no real case
        store is configured. Implementations with a real store configured (`GatewayBackend`
        with `OUTBOUND_IVR_CASES_TABLE` set) must raise `CaseNotFoundError`, not return `None`,
        when the case_id isn't in it -- the caller must never fall back to the demo case
        by mistake once a real store is in play.
        """
        return None


def _seed_from_cases_table(case_id: str) -> CaseStore | None:
    """When a cases table is configured (Outbound IVR on AWS), copy that case and its office
    into an in-memory store, so the call uses the case the dashboard started. Tools
    still write only in memory; this is a read. None if it can't be read."""
    if not os.environ.get("OUTBOUND_IVR_CASES_TABLE"):
        return None
    try:
        remote = case_store_from_env()
        case = remote.get_case(case_id)
        if case is None:
            logger.warning("case_not_in_table_using_demo_case", case_id=case_id)
            return None
        store = InMemoryCaseStore()
        store.put_case(case)
        office = remote.get_office(case["office_id"]) if case.get("office_id") else None
        if office:
            store.put_office(office)
        return store
    except Exception:
        logger.exception("cases_table_read_failed_using_demo_case", case_id=case_id)
        return None


def _seed_case_store(case_id: str) -> CaseStore:
    """An `InMemoryCaseStore` holding one case: from the cases table when one is
    configured, else seeded from `prompts.CASE`."""
    remote = _seed_from_cases_table(case_id)
    if remote is not None:
        return remote
    store = InMemoryCaseStore()
    store.put_case(
        default_case(
            case_id,
            payer=prompts.CASE["payer"],
            provider_office=prompts.CASE["provider_office"],
            ordering_provider=prompts.CASE["ordering_provider"],
            patient_name=prompts.CASE["patient_name"],
            patient_dob=prompts.CASE["patient_dob"],
            member_id=prompts.CASE["member_id"],
            auth_ref=prompts.CASE["auth_ref"],
            procedure=prompts.CASE["procedure"],
            submitted_on=prompts.CASE["submitted_on"],
            return_fax=prompts.CASE["return_fax"],
            portal=prompts.CASE["portal"],
            missing_items=prompts.CASE["missing_items"],
        )
    )
    return store


class LocalBackend(IntakeBackend):
    """`outbound_ivr.intake` on the call's in-memory record, plus `outbound_ivr.tools.business` on an
    `InMemoryCaseStore` seeded from `prompts.CASE` -- see the module docstring."""

    def __init__(
        self,
        record: dict[str, Any],
        case_id: str | None = None,
    ) -> None:
        self._record = record
        self._case_id = case_id or prompts.CASE["auth_ref"]
        self._store = _seed_case_store(self._case_id)

    async def record_item(self, *, case_id, call, key, value, confirmed) -> None:
        intake.record_item(self._record, key, value, confirmed)
        business.record_item(self._store, self._case_id, call, key, value, confirmed)

    async def escalate(self, *, case_id, call, reason, follow_up) -> dict[str, Any]:
        outcome = intake.escalate(self._record, reason, follow_up or None)
        business.escalate(self._store, self._case_id, call, reason, follow_up)
        return outcome

    async def complete(self, *, case_id, call, summary) -> dict[str, Any]:
        outcome = intake.complete(self._record, summary)
        business.complete(self._store, self._case_id, call, summary)
        return outcome

    async def transfer_to_team(self, *, case_id, call, reason, summary) -> dict[str, Any]:
        entry = intake.transfer_to_team(self._record, reason, summary)
        business.transfer_to_team(self._store, self._case_id, call, reason, summary)
        return entry

    async def mark_handoff_joined(
        self, *, case_id, call, reason, summary, identity
    ) -> dict[str, Any]:
        outcome = intake.mark_handoff_joined(self._record, reason, summary, identity)
        business.mark_handoff_joined(self._store, self._case_id, call, reason, summary, identity)
        return outcome

    def case_for_prompt(self) -> dict[str, Any] | None:
        return business.load_case(self._store, self._case_id)


# The Gateway's policy-session header (policies/README.md, "session-aware temporal
# conditions"): every tool call must carry it, stable across the whole phone call, so
# `transfer_to_team_once.dw`'s "once per session" rule can tell calls in the same
# session apart from a fresh one -- omitting it fails policy validation outright on an
# engine holding a temporal policy.
GATEWAY_POLICY_SESSION_HEADER = "x-amzn-bedrock-agentcore-policy-session-id"
# The Gateway lists each tool as "<target name>___<tool name>" (three underscores);
# the target is gateway_stack.py's GATEWAY_TARGET_NAME.
GATEWAY_TOOL_PREFIX = "OutboundIvrTools___"

# What `_GatewaySigV4Auth.auth_flow` raises on, and `GatewayBackend._call_tool` looks
# for in a swallowed-and-stringified tool error (see `GatewayPolicySessionConflict`'s
# docstring) -- a fixed marker, not pattern-matching on arbitrary server text.
_POLICY_SESSION_CONFLICT_MARKER = "AgentCore Gateway policy session conflict (409)"


def _tool_error_text(result: Any) -> str | None:
    """The error text of a failed MCP tool result, or None if it succeeded.

    `MCPClient.call_tool_sync` reports almost every failure as a normal return
    value with `status: "error"` rather than by raising, so this is the only place
    a dropped connection, a timeout, or a policy denial can be noticed at all.
    """
    if not isinstance(result, dict) or result.get("status") == "success":
        return None
    return "".join(
        block.get("text", "") for block in result.get("content") or [] if isinstance(block, dict)
    )


class GatewayToolCallFailed(RuntimeError):
    """A Gateway tool call came back `status: "error"` for something other than a
    policy-session conflict: a transport failure, a timeout, or a policy denial.

    Raised rather than logged so it reaches the caller. It used to be a WARNING and
    a returned value nobody inspected, which meant Outbound IVR told the caller and the
    model that an item was saved when the Gateway had refused or never answered.
    """

    def __init__(self, tool_name: str, detail: str) -> None:
        super().__init__(f"gateway tool {tool_name} failed: {detail[:300]}")
        self.tool_name = tool_name
        self.detail = detail


class GatewayPolicySessionConflict(RuntimeError):
    """The Gateway rejected the policy-session id with a 409 (`ConflictException`) --
    typically because a policy update just invalidated every open session on the
    engine. Caught by `GatewayBackend._to_thread`, which retries the call exactly once
    with a fresh session id.

    This can reach `GatewayBackend._call_tool` two ways: raised directly out of
    `with client:` if the 409 happens during the MCP session's own initialize
    handshake, or recovered from a `call_tool_sync` result whose text contains
    `_POLICY_SESSION_CONFLICT_MARKER` if it happens on the tool-call request itself --
    `strands.tools.mcp.MCPClient.call_tool_sync` swallows every exception from that
    request into an error-shaped result rather than raising, so the marker string is
    what actually survives the round trip, not this exception type.
    """


class _GatewaySigV4Auth(httpx.Auth):
    """SigV4-signs every request to the Gateway's MCP endpoint with the AgentCore
    Runtime's own execution-role credentials (the default boto3 session) -- the
    SHARED CONTRACT's replacement for `mcp-proxy-for-aws`, which pins an `awscrt`
    version that conflicts with Strands' own. Mirrors
    `bedrock_agentcore.tools.web_search_client`'s SigV4 pattern (`AWSRequest` +
    `botocore.auth.SigV4Auth`) -- the one AWS-blessed reference for signing a Gateway
    MCP call without `mcp-proxy-for-aws`.

    Credentials are resolved lazily, on the first signed request, never at
    construction -- so building a `GatewayBackend` (and importing this module) never
    needs AWS credentials configured; only actually calling a Gateway tool does.

    Also detects the Gateway's 409 `ConflictException` and raises
    `GatewayPolicySessionConflict`, since neither `httpx` nor the `mcp` client
    surfaces the response status code anywhere `GatewayBackend` could otherwise see it.
    """

    # Forces httpx to read the full request body before calling `auth_flow` (for both
    # the sync and async flows -- see `httpx.Auth.sync_auth_flow`/`async_auth_flow`),
    # so `request.content` below is always populated rather than raising
    # `RequestNotRead` on a request whose body hasn't been read yet.
    requires_request_body = True

    def __init__(self, *, service: str, region: str, session: Any = None) -> None:
        self._service = service
        self._region = region
        self._session = session

    def _credentials(self) -> Any:
        if self._session is None:
            import boto3

            self._session = boto3.Session()
        credentials = self._session.get_credentials()
        if credentials is None:
            raise RuntimeError(
                "OUTBOUND_IVR_TOOLS=gateway needs AWS credentials to sign the Gateway's MCP "
                "requests (the AgentCore Runtime's own execution role, via the default "
                "boto3 session) -- none are configured"
            )
        return credentials

    def auth_flow(self, request: httpx.Request):
        credentials = self._credentials()
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=dict(request.headers),
        )
        SigV4Auth(credentials.get_frozen_credentials(), self._service, self._region).add_auth(
            aws_request
        )
        for key, value in aws_request.headers.items():
            request.headers[key] = value
        response = yield request
        if response.status_code == 409:
            raise GatewayPolicySessionConflict(_POLICY_SESSION_CONFLICT_MARKER)


def gateway_region(url: str) -> str:
    """The region to sign for: the Gateway's own, from its URL
    (https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp), so a shell
    whose AWS_REGION points elsewhere still signs a valid request."""
    match = re.search(r"\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com", url)
    return match.group(1) if match else os.environ["AWS_REGION"]


class GatewayBackend(IntakeBackend):
    """`record_item`/`escalate`/`complete`/`transfer_to_team` over the AgentCore
    Gateway's MCP tools, SigV4-signed -- see the module docstring."""

    def __init__(
        self,
        record: dict[str, Any],
        case_id: str | None = None,
        *,
        mcp_client_factory: Callable[[str], Any] | None = None,
        case_store_factory: Callable[[], CaseStore] | None = None,
    ) -> None:
        self._case_id = case_id or prompts.CASE["auth_ref"]
        self._local = LocalBackend(record, case_id)
        self._mcp_client_factory = mcp_client_factory or self._default_mcp_client_factory
        # Read-only: GetItem on outbound-ivr-cases (and outbound-ivr-offices, via business.load_case)
        # through the agent's execution role. Only used when OUTBOUND_IVR_CASES_TABLE is set.
        self._case_store_factory = case_store_factory or case_store_from_env
        # The Gateway policy-session id for this call: lazily set to the first `call`
        # UUID seen (stable for the whole phone call, per GATEWAY_POLICY_SESSION_HEADER
        # above), then only ever changed by a 409 retry (`_fresh_policy_session_id`).
        self._session_id: str | None = None
        # One entered MCP session, reused for every tool call on this phone call.
        # It used to be one client per tool call, which meant a fresh TLS handshake,
        # a fresh SigV4 signature and a fresh MCP initialize on each one -- all of it
        # mid-conversation, with the person on the phone listening to the gap. The
        # lock serializes tool calls through the one session: `MCPClient` is not
        # documented as thread-safe and `_to_thread` can land two calls on different
        # threads if the model ever asks for two at once.
        self._client: Any | None = None
        self._client_session_id: str | None = None
        self._client_lock = threading.Lock()

    def case_for_prompt(self) -> dict[str, Any] | None:
        if not os.environ.get("OUTBOUND_IVR_CASES_TABLE"):
            # No real store configured (e.g. gateway tools tested without DynamoDB):
            # fall back to the fixed demo case, same as before.
            return None
        store = self._case_store_factory()
        case = business.load_case(store, self._case_id)
        if case is None:
            raise CaseNotFoundError(self._case_id)
        return case

    def _default_mcp_client_factory(self, session_id: str) -> Any:
        from strands.tools.mcp import MCPClient

        url = os.environ["OUTBOUND_IVR_GATEWAY_URL"]
        auth = _GatewaySigV4Auth(service="bedrock-agentcore", region=gateway_region(url))
        return MCPClient(
            url=url, headers={GATEWAY_POLICY_SESSION_HEADER: session_id}, auth_provider=auth
        )

    def _policy_session_id(self, call: str) -> str:
        if self._session_id is None:
            self._session_id = call or uuid.uuid4().hex
        return self._session_id

    def _fresh_policy_session_id(self, call: str) -> str:
        self._session_id = f"{call or 'call'}-{uuid.uuid4().hex[:8]}"
        return self._session_id

    def _entered_client(self, session_id: str) -> Any:
        """The live MCP session for `session_id`, opening one only if needed.

        A new session id means a new client: the policy-session header is set when
        the client is constructed, so the 409 retry cannot reuse the old one.
        """
        if self._client is not None and self._client_session_id != session_id:
            self._close_client()
        if self._client is None:
            client = self._mcp_client_factory(session_id)
            try:
                client.__enter__()
            except Exception as exc:
                # `MCPClient.start()` wraps anything that fails during the MCP
                # initialize handshake in MCPClientInitializationError, so a 409
                # raised by the SigV4 auth hook arrives here as the wrong type and
                # `_to_thread`'s retry never sees it. Unwrap one level and re-raise
                # the real thing. Deliberately narrow: any other init failure stays
                # as it is, because rotating the policy session id on an ordinary
                # transient failure would also reset the once-per-call transfer cap.
                cause = exc.__cause__
                if isinstance(cause, GatewayPolicySessionConflict):
                    raise cause from exc
                raise
            self._client = client
            self._client_session_id = session_id
        return self._client

    def _close_client(self) -> None:
        client, self._client, self._client_session_id = self._client, None, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.__exit__(None, None, None)

    async def aclose(self) -> None:
        """Release the MCP session at the end of the call. Safe to call twice."""
        await asyncio.to_thread(self._close_under_lock)

    def _close_under_lock(self) -> None:
        with self._client_lock:
            self._close_client()

    def _call_tool(self, name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any]:
        with self._client_lock:
            client = self._entered_client(session_id)
            try:
                result = client.call_tool_sync(
                    tool_use_id=f"{name}-{time.time_ns()}",
                    name=f"{GATEWAY_TOOL_PREFIX}{name}",
                    arguments=arguments,
                )
            except BaseException:
                # Never hand a session that just blew up to the next tool call.
                self._close_client()
                raise
            error_text = _tool_error_text(result)
            if error_text is not None:
                # Any non-success closes the session, not just a 409.
                # `MCPClient.call_tool_sync` swallows a dropped connection or a
                # timeout into an ordinary error result rather than raising, so the
                # `except` above never sees those, and the cached client used to
                # stay in place: one mid-call hiccup then broke every remaining
                # tool call on the phone call.
                self._close_client()
        if error_text is None:
            return result
        if _POLICY_SESSION_CONFLICT_MARKER in error_text:
            raise GatewayPolicySessionConflict(session_id)
        raise GatewayToolCallFailed(name, error_text)

    async def _to_thread(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        call = str(arguments.get("call") or "")
        session_id = self._policy_session_id(call)
        try:
            return await asyncio.to_thread(self._call_tool, name, arguments, session_id)
        except GatewayToolCallFailed as exc:
            # Retried on the SAME policy session id on purpose. Rotating it, which
            # is what the 409 path below does, would also reset the temporal
            # policy's count and hand the call a second warm transfer.
            logger.warning("gateway_tool_call_failed_retrying_once", tool=name, error=str(exc))
            return await asyncio.to_thread(self._call_tool, name, arguments, session_id)
        except GatewayPolicySessionConflict:
            fresh_session_id = self._fresh_policy_session_id(call)
            logger.warning(
                "gateway_tool_policy_session_rejected_retrying_once",
                tool=name,
                session_id=session_id,
                fresh_session_id=fresh_session_id,
            )
            return await asyncio.to_thread(self._call_tool, name, arguments, fresh_session_id)

    async def record_item(self, *, case_id, call, key, value, confirmed) -> None:
        await self._local.record_item(
            case_id=case_id, call=call, key=key, value=value, confirmed=confirmed
        )
        await self._to_thread(
            "record_item",
            {
                "case_id": case_id,
                "call": call,
                "key": key,
                "value": value,
                "confirmed": bool(confirmed),
            },
        )

    async def escalate(self, *, case_id, call, reason, follow_up) -> dict[str, Any]:
        outcome = await self._local.escalate(
            case_id=case_id, call=call, reason=reason, follow_up=follow_up
        )
        await self._to_thread(
            "escalate",
            {"case_id": case_id, "call": call, "reason": reason, "follow_up": follow_up or ""},
        )
        return outcome

    async def complete(self, *, case_id, call, summary) -> dict[str, Any]:
        outcome = await self._local.complete(case_id=case_id, call=call, summary=summary)
        await self._to_thread("complete", {"case_id": case_id, "call": call, "summary": summary})
        return outcome

    async def transfer_to_team(self, *, case_id, call, reason, summary) -> dict[str, Any]:
        outcome = await self._local.transfer_to_team(
            case_id=case_id, call=call, reason=reason, summary=summary
        )
        await self._to_thread(
            "transfer_to_team",
            {"case_id": case_id, "call": call, "reason": reason, "summary": summary},
        )
        return outcome

    async def mark_handoff_joined(
        self, *, case_id, call, reason, summary, identity
    ) -> dict[str, Any]:
        # Not a model-called tool, so there's nothing to forward over the Gateway for:
        # the local record and case store are the only places this needs to land.
        return await self._local.mark_handoff_joined(
            case_id=case_id, call=call, reason=reason, summary=summary, identity=identity
        )


# How long `transfer_to_team` waits for a human to join before telling the model no one
# answered (docs/escalation-handoff.md, part 3).
TEAM_HANDOFF_WAIT_SECONDS = float(os.environ.get("OUTBOUND_IVR_TEAM_HANDOFF_WAIT_SECONDS", "30"))


# How long a fresh handoff code stays valid in OUTBOUND_IVR_HANDOFF_TABLE before its TTL
# expires it: generous relative to how fast it's actually used (written, then sent as
# DTMF within the same `request_handoff` call, seconds later), since the only risk of
# too-long is a few stray rows outliving an aborted call before DynamoDB's TTL sweep
# catches them.
HANDOFF_CODE_TTL_SECONDS = int(os.environ.get("OUTBOUND_IVR_HANDOFF_CODE_TTL_SECONDS", "900"))

# The outbound trunk the office leg already dials through (outbound_ivr.voice.livekit_calls.
# dial_office's own OUTBOUND_IVR_TRUNK_NAME); the Evidence leg goes out through the same one,
# per docs/escalation-handoff.md part 2's "AWS (spec only, not built now)" -- this
# builds it.
DEFAULT_TRUNK_NAME = "outbound-ivr-chime-outbound"

# OUTBOUND_IVR_EVIDENCE_MODE: which AWS handoff path `AwsTeamDialer` runs. "connect" (the
# default, unset included -- backward compatible) keeps today's handoff-code-and-DTMF
# path into Amazon Connect's queue, below. "direct" dials `OUTBOUND_IVR_EVIDENCE_NUMBER`
# straight into the room with no handoff code and no DTMF: for a plain phone (a
# colleague's cell, not a Connect queue) there is no code to look up and nothing on the
# other end to read it, so Outbound IVR briefs whoever answers itself instead -- see
# `AwsTeamDialer._request_handoff_direct`. Any other value also falls back to "connect".
EVIDENCE_MODE_CONNECT = "connect"
EVIDENCE_MODE_DIRECT = "direct"


def _format_items_collected(items: dict[str, Any]) -> str:
    """One line per recorded item, for the handoff-codes table's `items_collected`
    column -- read back as a plain string by
    `src/lambdas/escalation_lookup/handler.py` and copied verbatim into a
    Connect contact attribute, so there's no structured format to match, just something
    legible."""
    if not items:
        return "(nothing recorded yet)"
    return "; ".join(f"{key}: {item.get('value')}" for key, item in items.items())


def _format_evidence_briefing(*, case_id: str | None, reason: str, items: dict[str, Any]) -> str:
    """What Outbound IVR says, word for word, to brief whoever answers in direct mode

    (`AwsTeamDialer._request_handoff_direct`): there's no Connect whisper flow on this
    leg to give a human a private briefing before they're bridged in (that only exists
    in connect mode, below), so Outbound IVR says the equivalent out loud itself once the
    colleague has joined. Built only from `case_id`, `reason`, and `items` -- never the
    model's own freeform `summary` -- so it can never repeat a caller's or patient's
    name the way a model-composed summary might (see the demo case's synthetic
    `patient_name` in `prompts.py`): `reason` is a short category phrase and `items` is
    the same name-free `_format_items_collected` line connect mode already stores for
    its own whisper flow.
    """
    case_ref = f"case {case_id}" if case_id else "this case"
    return (
        f"This is {case_ref}. {reason.strip()}. So far we have: {_format_items_collected(items)}."
    )


class AwsTeamDialer(TeamDialer):
    """The AWS warm-handoff path (docs/escalation-handoff.md, "AWS (spec only, not
    built now)" -- this builds it). Two modes, `OUTBOUND_IVR_EVIDENCE_MODE`
    (`EVIDENCE_MODE_CONNECT`/`EVIDENCE_MODE_DIRECT`):

    - **connect** (the default, unset included): write a fresh 6-digit handoff code
      into `OUTBOUND_IVR_HANDOFF_TABLE`, in exactly the shape
      `src/lambdas/escalation_lookup/handler.py` reads (`code` as the key,
      `case_id`/`items_collected`/`escalation_reason`/`summary`, TTL'd); `CreateSIPParticipant`
      a second leg into the *same room* (`room`, the call's own LiveKit room name)
      pointed at `OUTBOUND_IVR_EVIDENCE_NUMBER` through the same outbound trunk the office leg
      already uses; wait for it to answer; send the code as DTMF followed by `#`; then
      wait for that leg to actually join the room -- Connect bridges its agent into
      this same SIP leg, so "answered" and "a `team-` participant joined" are the one
      event here, not two.
    - **direct**: `OUTBOUND_IVR_EVIDENCE_NUMBER` is a plain phone -- a colleague's cell, not a
      Connect queue -- so there's no code to write and nothing on the other end to read
      DTMF: dial straight into the room, wait for the join, and hand back a `briefing`
      Outbound IVR says itself in place of a whisper flow that doesn't exist on this leg (see
      `_request_handoff_direct`, `_format_evidence_briefing`).

    `press_key` is `LiveKitTransport.press_key` (the same DTMF path the navigate phase's
    own `press_key` tool already uses to work the office's own menu), not a server-side
    API call: DTMF is room-scoped, not call-scoped, so it has to go out over Outbound IVR's own
    room connection (`room.local_participant.publish_dtmf`) -- `livekit-api` (the admin
    SDK `_dial_evidence_team` below uses for `CreateSIPParticipant`) has no equivalent
    server-side "send DTMF" call at all. Direct mode never calls it.
    """

    def __init__(
        self,
        *,
        join_waiter: Callable[[float], Any],
        press_key: Callable[[str], Any],
        case_id: str | None,
        evidence_number: str | None = None,
        evidence_mode: str | None = None,
        trunk_name: str | None = None,
        handoff_table: str | None = None,
        wait_seconds: float = TEAM_HANDOFF_WAIT_SECONDS,
    ) -> None:
        self._join_waiter = join_waiter
        self._press_key = press_key
        self._case_id = case_id
        self._evidence_number = evidence_number or os.environ.get("OUTBOUND_IVR_EVIDENCE_NUMBER")
        mode = (
            (evidence_mode or os.environ.get("OUTBOUND_IVR_EVIDENCE_MODE", EVIDENCE_MODE_CONNECT))
            .strip()
            .lower()
        )
        # Backward compatible: unset, "connect", or anything unrecognized all mean
        # "connect" -- only the literal "direct" switches modes.
        self._evidence_mode = mode if mode == EVIDENCE_MODE_DIRECT else EVIDENCE_MODE_CONNECT
        self._trunk_name = trunk_name or os.environ.get(
            "OUTBOUND_IVR_TRUNK_NAME", DEFAULT_TRUNK_NAME
        )
        self._handoff_table = handoff_table or os.environ.get(
            "OUTBOUND_IVR_HANDOFF_TABLE", "outbound-ivr-handoff-codes"
        )
        self._wait_seconds = wait_seconds

    async def request_handoff(self, *, room, summary, reason, items) -> HandoffResult:
        if not self._evidence_number:
            logger.error(
                "aws_team_dialer_missing_evidence_number",
                room=room,
            )
            return HandoffResult(joined=False)

        if self._evidence_mode == EVIDENCE_MODE_DIRECT:
            return await self._request_handoff_direct(room=room, reason=reason, items=items)

        code = await asyncio.to_thread(
            self._write_handoff_code, items=items, reason=reason, summary=summary
        )
        try:
            await self._dial_evidence_team(room)
        except Exception:
            logger.warning(
                "evidence_team_dial_failed",
                room=room,
                evidence_number=self._evidence_number,
                exc_info=True,
            )
            return HandoffResult(joined=False)

        for digit in f"{code}#":
            await self._press_key(digit)
        identity = await self._join_waiter(self._wait_seconds)
        return HandoffResult(joined=identity is not None, identity=identity)

    async def _request_handoff_direct(
        self, *, room: str, reason: str, items: dict[str, Any]
    ) -> HandoffResult:
        """`OUTBOUND_IVR_EVIDENCE_MODE=direct`: no handoff code, no DTMF -- dial
        `OUTBOUND_IVR_EVIDENCE_NUMBER` straight into `room` and wait for the join, same as
        `request_handoff`'s connect-mode path above does after it sends DTMF, but
        without a code to look up on the other end at all."""
        try:
            await self._dial_evidence_team(room)
        except Exception:
            logger.warning(
                "evidence_team_dial_failed",
                room=room,
                evidence_number=self._evidence_number,
                exc_info=True,
            )
            return HandoffResult(joined=False)

        identity = await self._join_waiter(self._wait_seconds)
        if identity is None:
            return HandoffResult(joined=False)
        briefing = _format_evidence_briefing(case_id=self._case_id, reason=reason, items=items)
        return HandoffResult(joined=True, identity=identity, briefing=briefing)

    def _write_handoff_code(self, *, items: dict[str, Any], reason: str, summary: str) -> str:
        """A fresh 6-digit code, retried on a (very unlikely) collision with a still-live
        one -- `PutItem` with `attribute_not_exists(code)` catches that without a
        separate read-then-write race."""
        import boto3
        from botocore.exceptions import ClientError

        table = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"]).Table(
            self._handoff_table
        )
        item_base = {
            "case_id": self._case_id or "",
            "items_collected": _format_items_collected(items),
            "escalation_reason": reason,
            "summary": summary,
            "expires_at": int(time.time()) + HANDOFF_CODE_TTL_SECONDS,
        }
        for _ in range(10):
            code = f"{secrets.randbelow(1_000_000):06d}"  # a bearer code for case data: CSPRNG, not random
            try:
                table.put_item(
                    Item={"code": code, **item_base},
                    ConditionExpression="attribute_not_exists(#code)",
                    ExpressionAttributeNames={"#code": "code"},
                )
                return code
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
        raise RuntimeError(
            f"could not find a free handoff code in {self._handoff_table!r} after 10 tries"
        )

    async def _dial_evidence_team(self, room: str) -> None:
        from outbound_ivr.voice import livekit_calls

        lkapi = livekit_calls.make_admin_client()
        try:
            await livekit_calls.dial_office(
                lkapi,
                room,
                livekit_calls.team_identity(room),
                extension=self._evidence_number,
                trunk_name=self._trunk_name,
            )
        finally:
            with contextlib.suppress(Exception):
                await lkapi.aclose()


def make_backend(record: dict[str, Any], case_id: str | None = None) -> IntakeBackend:
    """`local` (default) or `gateway`, per `OUTBOUND_IVR_TOOLS` (read fresh, not at import time)."""
    if os.environ.get("OUTBOUND_IVR_TOOLS", "local") == "gateway":
        return GatewayBackend(record, case_id)
    return LocalBackend(record, case_id)
