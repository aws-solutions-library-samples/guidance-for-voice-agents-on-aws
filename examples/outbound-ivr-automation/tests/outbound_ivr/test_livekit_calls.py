"""Unit tests for `outbound_ivr.voice.livekit_calls` against fakes of the `livekit`/`livekit-api`
SDK objects: no live LiveKit server, same approach `tests/test_livekit_transport.py` uses
for `LiveKitTransport`.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from outbound_ivr.voice import livekit_calls

CALL_ID = "call-1"
SIP_IDENTITY = f"sip-{CALL_ID}"
AGENT_IDENTITY = "outbound_ivr"


# --------------------------------------------------------------------------
# Fakes: livekit.api
# --------------------------------------------------------------------------


def _ns(**kw: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(**kw)


class FakeRoomServiceClient:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.deleted: list[str] = []
        self.participants: dict[str, list[str]] = {}

    async def create_room(self, req: Any) -> None:
        self.created.append(req)

    async def delete_room(self, req: Any) -> None:
        self.deleted.append(req.room)

    async def list_participants(self, req: Any) -> Any:
        return _ns(participants=[_ns(identity=i) for i in self.participants.get(req.room, [])])


class FakeSipServiceClient:
    def __init__(self, trunks: list[Any]) -> None:
        self.trunks = trunks
        self.dialed: list[Any] = []

    async def list_outbound_trunk(self, _req: Any) -> Any:
        return _ns(items=self.trunks)

    async def create_sip_participant(self, req: Any) -> None:
        self.dialed.append(req)


class FakeLiveKitAPI:
    def __init__(
        self, *, url: str, api_key: str, api_secret: str, trunks: list[Any] | None = None
    ) -> None:
        self.url, self.api_key, self.api_secret = url, api_key, api_secret
        self.room = FakeRoomServiceClient()
        self.sip = FakeSipServiceClient(trunks or [])
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeVideoGrants:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        for key, value in kw.items():
            setattr(self, key, value)


class FakeAccessToken:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key, self.api_secret = api_key, api_secret
        self._identity: str | None = None
        self._grants: Any = None

    def with_identity(self, identity: str) -> FakeAccessToken:
        self._identity = identity
        return self

    def with_grants(self, grants: Any) -> FakeAccessToken:
        self._grants = grants
        return self

    def to_jwt(self) -> str:
        return f"fake-jwt-{self._identity}"


def make_fake_api(
    trunks: list[Any] | None = None, created: list[FakeLiveKitAPI] | None = None
) -> types.SimpleNamespace:
    created = created if created is not None else []
    ns = types.SimpleNamespace()
    ns.CreateRoomRequest = lambda **kw: _ns(**kw)
    ns.ListParticipantsRequest = lambda **kw: _ns(**kw)
    ns.ListSIPOutboundTrunkRequest = lambda **kw: _ns(**kw)
    ns.CreateSIPParticipantRequest = lambda **kw: _ns(**kw)
    ns.SIPMediaConfig = lambda **kw: _ns(**kw)
    ns.DeleteRoomRequest = lambda **kw: _ns(**kw)
    ns.AccessToken = FakeAccessToken
    ns.VideoGrants = FakeVideoGrants

    def _lk_api(*, url: str, api_key: str, api_secret: str) -> FakeLiveKitAPI:
        inst = FakeLiveKitAPI(url=url, api_key=api_key, api_secret=api_secret, trunks=trunks)
        created.append(inst)
        return inst

    ns.LiveKitAPI = _lk_api
    return ns


class FakeProcess:
    """Stands in for asyncio.subprocess.Process: just enough for the lifecycle code's
    .returncode / .wait() / .terminate()."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self._done = asyncio.Event()

    def finish(self, code: int = 0) -> None:
        self.returncode = code
        self._done.set()

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode


# --------------------------------------------------------------------------
# resolve_api_secret / make_admin_client -- the ARN-aware admin client
# agentcore/livekit_app.py and outbound_ivr.voice.backend.AwsTeamDialer both need, since the
# Runtime only has LIVEKIT_API_SECRET_ARN, not the plain LIVEKIT_API_SECRET env var
# make_client() reads.
# --------------------------------------------------------------------------


def test_resolve_api_secret_falls_back_to_the_plain_env_var_without_an_arn(monkeypatch):
    monkeypatch.delenv("LIVEKIT_API_SECRET_ARN", raising=False)

    assert livekit_calls.resolve_api_secret() == livekit_calls.LIVEKIT_API_SECRET


def test_resolve_api_secret_reads_the_json_field_from_secrets_manager(monkeypatch):
    import boto3
    from moto import mock_aws

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with mock_aws():
        arn = boto3.client("secretsmanager", region_name="us-east-1").create_secret(
            Name="outbound-ivr/livekit-keys", SecretString='{"api_secret": "s3cr3t-from-arn"}'
        )["ARN"]
        monkeypatch.setenv("LIVEKIT_API_SECRET_ARN", arn)

        assert livekit_calls.resolve_api_secret() == "s3cr3t-from-arn"


def test_resolve_api_secret_caches_after_the_first_call(monkeypatch):
    import boto3
    from moto import mock_aws

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with mock_aws():
        arn = boto3.client("secretsmanager", region_name="us-east-1").create_secret(
            Name="outbound-ivr/livekit-keys", SecretString='{"api_secret": "first"}'
        )["ARN"]
        monkeypatch.setenv("LIVEKIT_API_SECRET_ARN", arn)

        first = livekit_calls.resolve_api_secret()
        # Even though the secret's value has since changed, the cached result is what
        # comes back -- Secrets Manager is a network call that never needs re-fetching
        # mid-process in production.
        boto3.client("secretsmanager", region_name="us-east-1").update_secret(
            SecretId=arn, SecretString='{"api_secret": "second"}'
        )
        second = livekit_calls.resolve_api_secret()

    assert first == "first"
    assert second == "first"


def test_make_admin_client_uses_the_resolved_secret_not_the_plain_env_var(monkeypatch):
    created: list[FakeLiveKitAPI] = []
    monkeypatch.setattr(livekit_calls, "_import_api", lambda: make_fake_api(created=created))
    monkeypatch.setattr(livekit_calls, "resolve_api_secret", lambda: "resolved-not-the-env-default")

    livekit_calls.make_admin_client()

    assert created[0].api_secret == "resolved-not-the-env-default"  # nosec B105 -- test fixture value, not a credential
    assert created[0].api_key == livekit_calls.LIVEKIT_API_KEY
    assert created[0].url == livekit_calls.LIVEKIT_URL


# --------------------------------------------------------------------------
# create_room / dial_office / delete_room
# --------------------------------------------------------------------------


async def test_create_room_sets_mode_and_case_id_metadata(monkeypatch):
    created: list[FakeLiveKitAPI] = []
    monkeypatch.setattr(livekit_calls, "_import_api", lambda: make_fake_api(created=created))
    lkapi = livekit_calls.make_client()

    await livekit_calls.create_room(lkapi, CALL_ID, mode="intake", case_id="AUTH-9")

    req = lkapi.room.created[0]
    assert req.name == CALL_ID
    import json

    assert json.loads(req.metadata) == {"mode": "intake", "case_id": "AUTH-9", "record": True}


async def test_create_room_omits_case_id_when_not_given(monkeypatch):
    monkeypatch.setattr(livekit_calls, "_import_api", make_fake_api)
    lkapi = livekit_calls.make_client()

    await livekit_calls.create_room(lkapi, CALL_ID)

    import json

    assert json.loads(lkapi.room.created[0].metadata) == {"mode": "full_call", "record": True}


async def test_create_room_passes_record_false_through(monkeypatch):
    monkeypatch.setattr(livekit_calls, "_import_api", make_fake_api)
    lkapi = livekit_calls.make_client()

    await livekit_calls.create_room(lkapi, CALL_ID, record=False)

    import json

    assert json.loads(lkapi.room.created[0].metadata) == {"mode": "full_call", "record": False}


async def test_dial_office_finds_the_named_trunk_and_dials(monkeypatch):
    trunks = [
        _ns(name="other-trunk", sip_trunk_id="ST_other"),
        _ns(name="spike-office", sip_trunk_id="ST_1"),
    ]
    monkeypatch.setattr(livekit_calls, "_import_api", lambda: make_fake_api(trunks=trunks))
    lkapi = livekit_calls.make_client()

    await livekit_calls.dial_office(
        lkapi, CALL_ID, SIP_IDENTITY, extension="200", trunk_name="spike-office"
    )

    req = lkapi.sip.dialed[0]
    assert req.sip_trunk_id == "ST_1"
    assert req.sip_call_to == "200"
    assert req.room_name == CALL_ID
    assert req.participant_identity == SIP_IDENTITY
    assert req.wait_until_answered is True


async def test_dial_office_raises_when_no_trunk_matches(monkeypatch):
    monkeypatch.setattr(livekit_calls, "_import_api", lambda: make_fake_api(trunks=[]))
    lkapi = livekit_calls.make_client()

    with pytest.raises(RuntimeError, match="spike-office"):
        await livekit_calls.dial_office(
            lkapi, CALL_ID, SIP_IDENTITY, extension="200", trunk_name="spike-office"
        )


async def test_delete_room_calls_through(monkeypatch):
    monkeypatch.setattr(livekit_calls, "_import_api", make_fake_api)
    lkapi = livekit_calls.make_client()

    await livekit_calls.delete_room(lkapi, CALL_ID)

    assert lkapi.room.deleted == [CALL_ID]


# --------------------------------------------------------------------------
# wait_for_agent_join
# --------------------------------------------------------------------------


async def test_wait_for_agent_join_returns_once_the_identity_appears(monkeypatch):
    monkeypatch.setattr(livekit_calls, "_import_api", make_fake_api)
    lkapi = livekit_calls.make_client()
    lkapi.room.participants[CALL_ID] = [AGENT_IDENTITY]
    proc = FakeProcess()

    await livekit_calls.wait_for_agent_join(
        lkapi, CALL_ID, proc, AGENT_IDENTITY, 1.0, poll_interval=0.01
    )


async def test_wait_for_agent_join_raises_if_the_process_exits_first(monkeypatch):
    monkeypatch.setattr(livekit_calls, "_import_api", make_fake_api)
    lkapi = livekit_calls.make_client()
    proc = FakeProcess()
    proc.finish(1)

    with pytest.raises(RuntimeError, match="exited"):
        await livekit_calls.wait_for_agent_join(
            lkapi, CALL_ID, proc, AGENT_IDENTITY, 1.0, poll_interval=0.01
        )


async def test_wait_for_agent_join_times_out_if_nobody_joins(monkeypatch):
    monkeypatch.setattr(livekit_calls, "_import_api", make_fake_api)
    lkapi = livekit_calls.make_client()
    proc = FakeProcess()

    with pytest.raises(RuntimeError, match="did not join"):
        await livekit_calls.wait_for_agent_join(
            lkapi, CALL_ID, proc, AGENT_IDENTITY, 0.03, poll_interval=0.01
        )


# --------------------------------------------------------------------------
# Evidence team: team_identity (docs/escalation-handoff.md)
# --------------------------------------------------------------------------


def test_team_identity_uses_the_team_prefix_and_is_fresh_each_time():
    first = livekit_calls.team_identity(CALL_ID)
    second = livekit_calls.team_identity(CALL_ID)
    assert first.startswith(livekit_calls.TEAM_IDENTITY_PREFIX)
    assert second.startswith(livekit_calls.TEAM_IDENTITY_PREFIX)
    assert first != second, "two Answer clicks must never collide on the same identity"
