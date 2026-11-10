"""Shared fixtures: one `OutboundIvrStack` app (the top-level stack plus its ten
nested children -- network, LiveKit, escalation, data, Monitor, gateway, the
Outbound IVR Runtime, FrontDesk, the dashboard, and ops; see outbound_ivr_stack.py's own
module docstring), with every file-based asset pointed at a throwaway stub
(the Outbound IVR Runtime's image directory and the dashboard's dist/ -- Monitor is
a plain S3 bucket now, no Lambda asset dir of its own to stub) -- these tests
never depend on `docker build`/`npm run build` actually running. FrontDesk is
unconditional: no `connectInstanceId`/`connectInstanceAlias` context is ever set here, by
design -- see test_outbound_ivr_stack.py's
`test_front_desk_is_always_built_with_no_connect_instance_id_context`.

The mock provider office itself (the Connect instance, its queue, agent user,
IVR flow, and claimed DID) is NOT part of this app any more -- it moved to
`OutboundIvrOfficeStack` (`stacks/office_stack.py`), a separate, independent
top-level `Stack` with its own tests (test_outbound_ivr_office_stack.py) and its own
throwaway `cdk.App()`, not built here. `OutboundIvrStack` only ever reads that
stack's SSM parameters (never a construct reference); see outbound_ivr_stack.py's
own module docstring for the contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infrastructure"))

# These tests synth without real AWS credentials, so stacks.constants (which refuses
# to fall back to a placeholder account/Region) needs explicit values. Set only if
# unset, so a developer's real CDK_DEFAULT_ACCOUNT/CDK_DEFAULT_REGION (or an explicit
# OUTBOUND_IVR_ACCOUNT_ID/OUTBOUND_IVR_REGION override) still wins.
os.environ.setdefault("OUTBOUND_IVR_ACCOUNT_ID", "111111111111")
os.environ.setdefault("OUTBOUND_IVR_REGION", "us-east-1")

from stacks.constants import ACCOUNT_ID, REGION
from stacks.outbound_ivr_stack import OutboundIvrStack

TEST_CELL_NUMBER = "+15555550199"
TEST_OFFICE_NUMBER = "+15555550198"
TEST_AGENT_EMAIL = "test-agent@example.com"

# The real, committed office IVR flow (connect/README.md documents how it's
# regenerated) -- used by test_outbound_ivr_office_stack.py as an explicit
# `contactFlowPath` context value rather than relied on by default, so those
# tests stay correct even if office_stack.py's own default path ever moves.
# Two parents up from this file (tests/infrastructure/ -> tests/ -> example root).
EXAMPLE_ROOT = Path(__file__).resolve().parents[2]
OFFICE_FLOW_PATH = (
    EXAMPLE_ROOT / "infrastructure" / "connect" / "flows" / "outbound-ivr-mock-provider-ivr.json"
)

# Action types from the Connect flow-language reference, and the flow types
# that allow them (docs.aws.amazon.com/connect/latest/devguide/
# flow-language-actions.html). Connect rejects anything else at create time,
# which synth never catches -- shared by test_escalation_stack.py's three
# flows and test_outbound_ivr_office_stack.py's office flow, both built from the
# same reference.
ALLOWED_CONNECT_FLOW_ACTIONS = {
    "CONTACT_FLOW": {
        "UpdateContactTextToSpeechVoice",
        "UpdateFlowLoggingBehavior",
        "GetParticipantInput",
        "MessageParticipant",
        "UpdateContactAttributes",
        "InvokeLambdaFunction",
        "UpdateContactEventHooks",
        "UpdateContactTargetQueue",
        "TransferContactToQueue",
        # "This action is available in every type of flow"
        # (flow-control-actions-compare.html).
        "Compare",
        "DisconnectParticipant",
    },
    "CUSTOMER_QUEUE": {
        "MessageParticipantIteratively",
        "MessageParticipant",
        "DisconnectParticipant",
        "EndFlowExecution",
    },
    "AGENT_WHISPER": {"UpdateContactTextToSpeechVoice", "MessageParticipant", "EndFlowExecution"},
}


def flatten_token_string(value) -> str:
    """A CFN property that embeds a token (an ARN stitched into a Connect flow's
    Content, for instance) synthesizes as an Fn::Join of string fragments and
    intrinsics rather than a plain string. Flatten to one string, substituting a
    placeholder for each intrinsic, so the flow JSON can still be parsed and its
    structure checked without a CloudFormation client to resolve the token."""
    if isinstance(value, str):
        return value
    parts = value["Fn::Join"][1]
    # Bare "<token>", no extra quote characters: the literal fragments on either
    # side of the token already carry the real quote marks (the token always
    # sits inside a JSON string value in these flows).
    return "".join(part if isinstance(part, str) else "<token>" for part in parts)


def parse_flow_content(resource: dict) -> dict:
    return json.loads(flatten_token_string(resource["Properties"]["Content"]))


def ssm_value_parameters(template: Template) -> dict[str, str]:
    """logical id -> SSM path, for every `AWS::SSM::Parameter::Value<String>`
    template parameter this template declares -- the
    `ssm.StringParameter.from_string_parameter_name(...).string_value`
    pattern outbound_ivr_stack.py uses to read office_stack.py's SSM contract (and
    livekit_stack.py already uses for the nine `/outbound-ivr/voice/*` parameters):
    a CloudFormation dynamic reference CloudFormation itself resolves at
    *deploy* time, never a synth-time `value_from_lookup`. Excludes CDK's own
    synthesizer-injected `BootstrapVersion` parameter (every stack gets one;
    it is unrelated to any application-level contract)."""
    return {
        logical_id: p["Default"]
        for logical_id, p in template.to_json().get("Parameters", {}).items()
        if p.get("Type") == "AWS::SSM::Parameter::Value<String>"
        and logical_id != "BootstrapVersion"
    }


def nested_stack_local_parameter_for(
    parent_template: Template, nested_stack_logical_id_prefix: str, parent_parameter_logical_id: str
) -> str | None:
    """The auto-generated local parameter name CDK gives a nested stack when a
    value defined in the PARENT stack's own scope (here, one of
    `ssm_value_parameters`'s SSM-backed template parameters) is passed into
    the nested stack's constructor: the nested stack resource's own
    `Parameters` property (on its `AWS::CloudFormation::Stack` entry in the
    parent template) maps this name to `{"Ref": parent_parameter_logical_id}`.
    Returns None if this nested stack never receives that parent parameter at
    all."""
    nested = parent_template.find_resources("AWS::CloudFormation::Stack")
    matches = [r for lid, r in nested.items() if lid.startswith(nested_stack_logical_id_prefix)]
    if not matches:
        raise AssertionError(
            f"no nested stack whose logical id starts with {nested_stack_logical_id_prefix!r}"
        )
    (resource,) = matches
    parameters = resource["Properties"].get("Parameters") or {}
    for local_name, value in parameters.items():
        if isinstance(value, dict) and value.get("Ref") == parent_parameter_logical_id:
            return local_name
    return None


def assert_flow_has_no_dangling_references(flow: dict) -> None:
    ids = {a["Identifier"] for a in flow["Actions"]}
    assert flow["StartAction"] in ids
    for action in flow["Actions"]:
        transitions = action.get("Transitions", {})
        refs = []
        # NextAction "" ends the flow (a successful TransferContactToQueue).
        if transitions.get("NextAction"):
            refs.append(transitions["NextAction"])
        refs += [e["NextAction"] for e in transitions.get("Errors", [])]
        refs += [c["NextAction"] for c in transitions.get("Conditions", [])]
        for ref in refs:
            assert ref in ids, f"{action['Type']} points at an action identifier not in this flow"


@pytest.fixture(scope="session")
def stub_dirs(tmp_path_factory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("outbound-ivr-stub-assets")

    runtime_image = base / "runtime-image"
    (runtime_image / "src" / "agentcore").mkdir(parents=True)
    (runtime_image / "src" / "agentcore" / "Dockerfile").write_text("FROM scratch\n")

    dashboard_dist = base / "dashboard-dist"
    dashboard_dist.mkdir()
    (dashboard_dist / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div></body></html>'
    )

    return {
        "runtime_image": runtime_image,
        "dashboard_dist": dashboard_dist,
    }


@pytest.fixture(scope="session")
def app_context(stub_dirs) -> dict[str, str]:
    # officeNumber/contactFlowPath/claimOfficeNumber are NOT read by OutboundIvrStack
    # any more -- the mock office (and its claimed-number and office-flow
    # context flags) moved out entirely to the independent OutboundIvrOfficeStack
    # (stacks/office_stack.py), built and tested separately
    # (test_outbound_ivr_office_stack.py), never through this fixture. officeNumber
    # is kept only as a value here so AgentRuntimeNestedStack's own direct,
    # standalone constructions in test_agent_runtime_stack.py (which still
    # read context officeNumber as their fallback) have a TEST_OFFICE_NUMBER
    # to import; OutboundIvrStack's own build always overrides it with the real
    # SSM-sourced value regardless (outbound_ivr_stack.py's own module docstring).
    return {
        "runtimeImageDir": str(stub_dirs["runtime_image"]),
        "dashboardDistDir": str(stub_dirs["dashboard_dist"]),
        # Absolute, so these resolve the same way regardless of pytest's cwd (the
        # stacks' own defaults are relative to `infrastructure/`, the CDK app's own
        # working directory, which `cdk synth` always runs from but `pytest` need not).
        "toolsLambdaDir": str(EXAMPLE_ROOT / "src" / "lambdas" / "tools"),
        "apiLambdaDir": str(EXAMPLE_ROOT / "src" / "lambdas" / "api"),
        "outboundIvrPackageDir": str(EXAMPLE_ROOT / "src" / "outbound_ivr"),
        "policiesDir": str(EXAMPLE_ROOT / "infrastructure" / "policies"),
        "testCellNumber": TEST_CELL_NUMBER,
        "officeNumber": TEST_OFFICE_NUMBER,
        "agentEmail": TEST_AGENT_EMAIL,
        # True here (unlike infra/'s claimOfficeNumber, left at its False
        # default in infra/tests/conftest.py) so the DID-claim custom resource
        # -- one of the resources the task asked for by name -- is actually
        # exercised and cdk-nag-checked, not left synth-untested.
        "claimEvidenceNumber": "true",
    }


@pytest.fixture(scope="session")
def stacks(app_context):
    """Build the app once per test session, the way app.py does."""
    app = cdk.App(context=app_context)
    env = cdk.Environment(account=ACCOUNT_ID, region=REGION)

    outbound_ivr = OutboundIvrStack(app, "TestOutboundIvrStack", env=env)

    return {
        "app": app,
        "outbound_ivr": outbound_ivr,
        "network": outbound_ivr.network,
        "livekit": outbound_ivr.livekit,
        "escalation": outbound_ivr.escalation,
        "data": outbound_ivr.data,
        "monitor": outbound_ivr.monitor,
        "gateway": outbound_ivr.gateway,
        "agent_runtime": outbound_ivr.agent_runtime,
        "dashboard": outbound_ivr.dashboard,
        "front_desk": outbound_ivr.front_desk,
        "ops": outbound_ivr.ops,
    }


@pytest.fixture(scope="session")
def outbound_ivr_template(stacks) -> Template:
    return Template.from_stack(stacks["outbound_ivr"])


@pytest.fixture(scope="session")
def network_template(stacks) -> Template:
    return Template.from_stack(stacks["network"])


@pytest.fixture(scope="session")
def livekit_template(stacks) -> Template:
    return Template.from_stack(stacks["livekit"])


@pytest.fixture(scope="session")
def escalation_template(stacks) -> Template:
    return Template.from_stack(stacks["escalation"])


@pytest.fixture(scope="session")
def data_template(stacks) -> Template:
    return Template.from_stack(stacks["data"])


@pytest.fixture(scope="session")
def monitor_template(stacks) -> Template:
    return Template.from_stack(stacks["monitor"])


@pytest.fixture(scope="session")
def gateway_template(stacks) -> Template:
    return Template.from_stack(stacks["gateway"])


@pytest.fixture(scope="session")
def agent_runtime_template(stacks) -> Template:
    return Template.from_stack(stacks["agent_runtime"])


@pytest.fixture(scope="session")
def dashboard_template(stacks) -> Template:
    return Template.from_stack(stacks["dashboard"])


@pytest.fixture(scope="session")
def front_desk_template(stacks) -> Template:
    return Template.from_stack(stacks["front_desk"])


def connect_statements(template: Template) -> list[dict]:
    """Every IAM policy statement in the template that grants a connect: action."""
    found = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = [actions] if isinstance(actions, str) else actions
            if any(a.startswith("connect:") for a in actions):
                found.append(statement)
    return found


# ----------------------------------------------------------------------------
# OutboundIvrVoiceStack (the permanent Chime SDK Voice Connector + EIP pool).
# Built as its own throwaway `cdk.App()`, same pattern as the `stacks` fixture
# above but for test_voice_stack.py only.
# ----------------------------------------------------------------------------
from stacks.voice_stack import VoiceStack


@pytest.fixture(scope="session")
def voice_stacks(app_context):
    app = cdk.App(context=app_context)
    env = cdk.Environment(account=ACCOUNT_ID, region=REGION)
    voice = VoiceStack(app, "TestOutboundIvrVoiceStack", env=env)
    return {"app": app, "voice": voice}


@pytest.fixture(scope="session")
def voice_template(voice_stacks) -> Template:
    return Template.from_stack(voice_stacks["voice"])
