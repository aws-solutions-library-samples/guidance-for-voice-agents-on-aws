"""Shared LiveKit call lifecycle: create a room, dial the office, and run the
Evidence-team handoff. See `outbound_ivr.voice.livekit_transport`'s module docstring ("Room
contract") for what the agent side expects to find once it joins the room this module
creates.

The `livekit`/`livekit-api` packages are imported lazily (`_import_rtc`/`_import_api`
below, the same pattern `livekit_transport.py` uses), so importing this module never
requires the "livekit" extra to be installed. Only actually calling one of these
functions needs them.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import time
import uuid
from typing import Any

import structlog

logger = structlog.get_logger("outbound_ivr.voice.livekit_calls")

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")

# Overridable, but there is only one office in the deployed path (OutboundIvrOfficeStack).
# Same env var and default as outbound_ivr.voice.backend's own DEFAULT_TRUNK_NAME and
# src/agentcore/livekit_app.py's -- callers in the deployed path always pass
# `trunk_name` explicitly rather than relying on this default, but it stays
# consistent with the rest of the app for anyone calling `dial_office` directly.
TRUNK_NAME = os.environ.get("OUTBOUND_IVR_TRUNK_NAME", "outbound-ivr-chime-outbound")
OFFICE_EXTENSION = os.environ.get("OUTBOUND_IVR_LIVEKIT_OFFICE_EXTENSION", "200")
AGENT_IDENTITY = "outbound_ivr"

JOIN_WAIT_S = 20.0  # how long to wait for the agent to join the room before dialing out
CALL_TIMEOUT_S = 420.0  # safety ceiling on the whole call

# How long livekit-sip waits for RTP on a leg before tearing the call down as dead, set
# per call via `CreateSIPParticipantRequest.media.media_timeout`.
#
# This override DOES take effect on the image deployed to AWS: a measured call logged
# `triggering media timeout` with `timeout: "2m0s"`, which is this value, not the
# `media_timeout: 10s` in the deployed server's own config -- only the per-call field
# actually moves it.
#
# That matters because this timeout is what ends a call when the far end hangs up without
# a BYE the bridge sees, which is the normal case through the Voice Connector: 120 seconds
# is far too long to leave a human sitting on a dead line. Lowering it is what lets
# livekit-sip itself remove the participant, which surfaces as `participant_disconnected`
# -- the real hangup signal, rather than the audio-silence backstop in
# `outbound_ivr.voice.agent._wait_for_handoff_to_end` having to infer it.
#
# The default stays at 120 for a plain, no-env-vars-set run: real telephony (Connect, a
# mobile) sends RTP continuously while off-hook, so the AWS deployment sets this low --
# see OUTBOUND_IVR_SIP_MEDIA_TIMEOUT_S in the agent runtime stack.
SIP_MEDIA_TIMEOUT_S = float(os.environ.get("OUTBOUND_IVR_SIP_MEDIA_TIMEOUT_S", "120"))


def _import_rtc() -> Any:
    """The real `livekit.rtc` module, imported on first use. See `livekit_transport.py`'s
    `_import_rtc` for why this is lazy and monkeypatched (not the `livekit` package
    itself) in tests."""
    try:
        from livekit import rtc
    except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
        raise ImportError(
            "outbound_ivr.voice.livekit_calls needs the 'livekit' package. Install it with "
            "`uv sync --extra strands --extra livekit`."
        ) from exc
    return rtc


def _import_api() -> Any:
    """The real `livekit.api` module, imported on first use. See `_import_rtc`."""
    try:
        from livekit import api
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "outbound_ivr.voice.livekit_calls needs the 'livekit-api' package. Install it with "
            "`uv sync --extra strands --extra livekit`."
        ) from exc
    return api


def make_client() -> Any:
    """One `livekit.api.LiveKitAPI` client, using the plain `LIVEKIT_API_SECRET` env var.
    Callers are responsible for `await client.aclose()`."""
    api = _import_api()
    return api.LiveKitAPI(url=LIVEKIT_URL, api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET)


_cached_api_secret: str | None = None


def resolve_api_secret() -> str:
    """The LiveKit API secret: `LIVEKIT_API_SECRET_ARN` (Secrets Manager, JSON field
    `api_secret` -- set by the AWS deployment) if set, else the plain
    `LIVEKIT_API_SECRET` env var (`make_client()`'s own default, "secret"). Resolved
    once per process and cached: Secrets Manager is a network call, and this never
    changes mid-process.

    `make_client()` itself is untouched and keeps reading the plain env var directly --
    a caller with no ARN to resolve and no AWS credentials to resolve one with must not
    pay for (or depend on) this function at all.
    """
    global _cached_api_secret
    if _cached_api_secret is not None:
        return _cached_api_secret
    secret_arn = os.environ.get("LIVEKIT_API_SECRET_ARN")
    if secret_arn:
        import boto3

        # Explicit, like every other AWS client this package builds: AgentCore
        # Runtime always sets AWS_REGION, and a client left to boto3's own default
        # resolution depends on an ambient AWS_DEFAULT_REGION/profile that need not
        # be there.
        response = boto3.client(
            "secretsmanager", region_name=os.environ["AWS_REGION"]
        ).get_secret_value(SecretId=secret_arn)
        _cached_api_secret = json.loads(response["SecretString"])["api_secret"]
    else:
        _cached_api_secret = LIVEKIT_API_SECRET
    return _cached_api_secret


def make_admin_client() -> Any:
    """Like `make_client()`, but authenticated with `resolve_api_secret()` (ARN-aware)
    instead of the plain `LIVEKIT_API_SECRET` env var -- for callers running where the
    secret only exists in Secrets Manager (`agentcore/livekit_app.py`,
    `outbound_ivr.voice.backend.AwsTeamDialer`). Callers are responsible for `await
    client.aclose()`, same as `make_client()`."""
    api = _import_api()
    return api.LiveKitAPI(url=LIVEKIT_URL, api_key=LIVEKIT_API_KEY, api_secret=resolve_api_secret())


# --------------------------------------------------------------------------
# Call lifecycle primitives: each one does exactly one LiveKit API call; the
# sequencing lives in the caller (agentcore/livekit_app.py's `_run_call`).
# --------------------------------------------------------------------------


async def create_room(
    lkapi: Any,
    call_id: str,
    *,
    mode: str = "full_call",
    case_id: str | None = None,
    record: bool = True,
) -> None:
    """Create one room for this call, with the metadata `LiveKitTransport.handshake()`
    reads for mode/case id/record (see its "Room contract"). `record` defaults on and
    tells `LiveKitTransport` whether to start a `CallRecorder` for this call."""
    api = _import_api()
    metadata: dict[str, Any] = {"mode": mode, "record": record}
    if case_id:
        metadata["case_id"] = case_id
    await lkapi.room.create_room(api.CreateRoomRequest(name=call_id, metadata=json.dumps(metadata)))


async def wait_for_agent_join(
    lkapi: Any,
    call_id: str,
    proc: asyncio.subprocess.Process,
    identity: str = AGENT_IDENTITY,
    timeout: float = JOIN_WAIT_S,
    *,
    poll_interval: float = 0.3,
) -> None:
    """Poll `ListParticipants` for `identity`; raise if `proc` exits first or this times out."""
    api = _import_api()
    deadline = time.monotonic() + timeout
    while True:
        if proc.returncode is not None:
            raise RuntimeError(
                f"agent process exited (code {proc.returncode}) before joining room {call_id!r}"
            )
        resp = await lkapi.room.list_participants(api.ListParticipantsRequest(room=call_id))
        if any(p.identity == identity for p in resp.participants):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"agent did not join room {call_id!r} within {timeout:.0f}s")
        await asyncio.sleep(poll_interval)


async def dial_office(
    lkapi: Any,
    call_id: str,
    sip_identity: str,
    *,
    extension: str = OFFICE_EXTENSION,
    trunk_name: str = TRUNK_NAME,
    media_timeout: float = SIP_MEDIA_TIMEOUT_S,
) -> None:
    """Dial `extension` on `trunk_name` into the room, waiting for the office to answer."""
    api = _import_api()
    trunks = await lkapi.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
    matches = [t for t in trunks.items if t.name == trunk_name]
    if not matches:
        raise RuntimeError(f"no {trunk_name!r} outbound trunk found")
    trunk_id = matches[0].sip_trunk_id
    await lkapi.sip.create_sip_participant(
        api.CreateSIPParticipantRequest(
            sip_trunk_id=trunk_id,
            sip_call_to=extension,
            room_name=call_id,
            participant_identity=sip_identity,
            wait_until_answered=True,
            media=api.SIPMediaConfig(media_timeout=datetime.timedelta(seconds=media_timeout)),
        )
    )


async def delete_room(lkapi: Any, call_id: str) -> None:
    api = _import_api()
    await lkapi.room.delete_room(api.DeleteRoomRequest(room=call_id))


# --------------------------------------------------------------------------
# Evidence team: identities for the SIP leg that dials out to a human once the agent
# asks for one (docs/escalation-handoff.md, part 1). This participant is meant to be
# seen and heard by everyone else in the room -- not hidden, and it can publish its
# own audio.
# --------------------------------------------------------------------------

# Same prefix docs/escalation-handoff.md gives the agent side's far-end-exclusion guard
# (`LiveKitTransport._maybe_adopt_far_end`): kept here as a plain literal, not imported
# from `livekit_transport`, because that module doesn't define it yet -- nothing here
# needs it to; a browser or SIP participant with this identity prefix is a contract
# between the two sides of docs/escalation-handoff.md, not a shared constant.
TEAM_IDENTITY_PREFIX = "team-"


def team_identity(call_id: str) -> str:  # call_id argument kept for a legible call site
    """A fresh identity for one Evidence team browser leg: always `TEAM_IDENTITY_PREFIX`
    plus a fresh uuid, so two Answer clicks (or a retry after no answer) never collide on
    the same identity in the same room."""
    return f"{TEAM_IDENTITY_PREFIX}{uuid.uuid4()}"
