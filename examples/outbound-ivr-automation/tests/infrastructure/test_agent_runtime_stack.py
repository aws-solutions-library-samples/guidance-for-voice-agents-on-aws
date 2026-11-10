"""Assertions for outbound-ivr-agent-runtime: the real Outbound IVR Runtime's IAM is
scoped exactly to the shared contract's list (Nova Sonic on one model, S3 writes
on two prefixes, one DynamoDB PutItem apiece on the events/handoff tables, one
secret read), and its env vars match the shared contract exactly.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.constants import ACCOUNT_ID, REGION
from stacks.outbound_ivr_stack import OutboundIvrStack

from infrastructure.conftest import (
    EXAMPLE_ROOT,
    TEST_AGENT_EMAIL,
    TEST_CELL_NUMBER,
    TEST_OFFICE_NUMBER,
    nested_stack_local_parameter_for,
    ssm_value_parameters,
)

_LAMBDA_AND_POLICY_CONTEXT = {
    "toolsLambdaDir": str(EXAMPLE_ROOT / "src" / "lambdas" / "tools"),
    "apiLambdaDir": str(EXAMPLE_ROOT / "src" / "lambdas" / "api"),
    "outboundIvrPackageDir": str(EXAMPLE_ROOT / "src" / "outbound_ivr"),
    "policiesDir": str(EXAMPLE_ROOT / "infrastructure" / "policies"),
}


@pytest.fixture(scope="module")
def direct_evidence_mode_template(stub_dirs) -> Template:
    """A standalone build with `-c evidenceMode=direct`, the AwsTeamDialer mode
    the design plan's shared contract adds (dials OUTBOUND_IVR_EVIDENCE_NUMBER straight
    into the room, no Connect handoff code) -- the shared session `stacks` fixture
    leaves evidenceMode unset, to keep the default-context assertions in this file
    about the "connect" default meaningful."""
    app = cdk.App(
        context={
            **_LAMBDA_AND_POLICY_CONTEXT,
            "runtimeImageDir": str(stub_dirs["runtime_image"]),
            "dashboardDistDir": str(stub_dirs["dashboard_dist"]),
            "testCellNumber": TEST_CELL_NUMBER,
            "officeNumber": TEST_OFFICE_NUMBER,
            "agentEmail": TEST_AGENT_EMAIL,
            "claimEvidenceNumber": "true",
            "evidenceMode": "direct",
        }
    )
    env = cdk.Environment(account=ACCOUNT_ID, region=REGION)
    outbound_ivr = OutboundIvrStack(app, "TestDirectEvidenceModeOutboundIvr", env=env)
    return Template.from_stack(outbound_ivr.agent_runtime)


def test_evidence_mode_direct_is_passed_through(direct_evidence_mode_template) -> None:
    (runtime,) = direct_evidence_mode_template.find_resources(
        "AWS::BedrockAgentCore::Runtime"
    ).values()
    assert runtime["Properties"]["EnvironmentVariables"]["OUTBOUND_IVR_EVIDENCE_MODE"] == "direct"


def _standalone_agent_runtime_stack(stub_dirs, *, office_phone_number, office_number_context):
    """`office_phone_number` (FrontDesk's own claimed DID, see frontdesk_stack.py) is
    a constructor argument this stack should prefer over context `officeNumber`
    when given one. `outbound_ivr_stack.py` does
    not actually pass a real one today -- wiring FrontDesk's output back into
    AgentRuntime, which is built *before* FrontDesk, would close a
    AgentRuntime -> FrontDesk -> Dashboard -> AgentRuntime cycle (see
    outbound_ivr_stack.py's own comment) -- so this constructs the nested stack
    directly instead of going through OutboundIvrStack, to test the kwarg's own
    precedence contract independent of whether/how the caller ends up wiring
    it.
    """
    import aws_cdk as cdk
    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import aws_ec2 as ec2
    from aws_cdk import aws_s3 as s3
    from stacks.agent_runtime_stack import AgentRuntimeNestedStack

    context = {"runtimeImageDir": str(stub_dirs["runtime_image"])}
    if office_number_context is not None:
        context["officeNumber"] = office_number_context
    app = cdk.App(context=context)
    parent = cdk.Stack(app, "TestParent", env=cdk.Environment(account=ACCOUNT_ID, region=REGION))
    vpc = ec2.Vpc(
        parent,
        "Vpc",
        max_azs=1,
        nat_gateways=0,
        subnet_configuration=[
            ec2.SubnetConfiguration(
                name="priv", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
            )
        ],
    )
    key_attr = dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING)
    runtime = AgentRuntimeNestedStack(
        parent,
        "Runtime",
        vpc=vpc,
        private_subnets=ec2.SubnetSelection(subnets=vpc.isolated_subnets),
        events_table=dynamodb.Table(parent, "Events", partition_key=key_attr),
        cases_table=dynamodb.Table(parent, "Cases", partition_key=key_attr),
        offices_table=dynamodb.Table(parent, "Offices", partition_key=key_attr),
        artifacts_bucket=s3.Bucket(parent, "Artifacts"),
        handoff_table=dynamodb.Table(parent, "Handoff", partition_key=key_attr),
        evidence_number=None,
        livekit_host="livekit.outbound_ivr.internal",
        livekit_api_key="test-key",
        livekit_api_secret_arn=f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:test-xxxxxx",
        gateway_url="https://example.com/mcp",
        office_phone_number=office_phone_number,
    )
    return Template.from_stack(runtime)


def test_claimed_office_phone_number_kwarg_overrides_the_context_fallback(stub_dirs) -> None:
    template = _standalone_agent_runtime_stack(
        stub_dirs, office_phone_number="+15550009999", office_number_context="+15555550101"
    )
    (runtime,) = template.find_resources("AWS::BedrockAgentCore::Runtime").values()
    assert (
        runtime["Properties"]["EnvironmentVariables"]["OUTBOUND_IVR_OFFICE_NUMBER"]
        == "+15550009999"
    )


def test_with_no_claimed_office_phone_number_the_context_fallback_still_synthesizes(
    stub_dirs,
) -> None:
    """A deploy with no claimed office DID (claimOfficeNumber left at its False
    default) must still synthesize, falling back to context officeNumber."""
    template = _standalone_agent_runtime_stack(
        stub_dirs, office_phone_number=None, office_number_context="+15555550101"
    )
    (runtime,) = template.find_resources("AWS::BedrockAgentCore::Runtime").values()
    assert (
        runtime["Properties"]["EnvironmentVariables"]["OUTBOUND_IVR_OFFICE_NUMBER"]
        == "+15555550101"
    )


def _runtime_resource(agent_runtime_template):
    (runtime,) = agent_runtime_template.find_resources("AWS::BedrockAgentCore::Runtime").values()
    return runtime


def test_runtime_env_vars_match_the_shared_contract(agent_runtime_template) -> None:
    runtime = _runtime_resource(agent_runtime_template)
    env = runtime["Properties"]["EnvironmentVariables"]
    # Nothing reads OUTBOUND_IVR_TRANSPORT any more (the Runtime is LiveKit-only), so it is not set.
    assert "OUTBOUND_IVR_TRANSPORT" not in env
    assert env["OUTBOUND_IVR_LEVELS"] == "0"
    assert env["OUTBOUND_IVR_TOOLS"] == "gateway"
    assert "OUTBOUND_IVR_GATEWAY_URL" in env
    # Keeps prompt/tool-call content on Strands' spans for Agent traces/
    # Evaluations to read -- see agent_runtime_stack.py's env-block comment.
    assert env["AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT"] == "true"
    assert env["OUTBOUND_IVR_TEAM_DIALER"] == "aws"
    assert env["OUTBOUND_IVR_TRUNK_NAME"] == "outbound-ivr-chime-outbound"
    assert env["OUTBOUND_IVR_SONIC_MODEL"] == "amazon.nova-2-5-sonic"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["LIVEKIT_URL"].startswith("ws://")
    assert env["LIVEKIT_URL"].endswith(":7880")
    assert "LIVEKIT_API_KEY" in env
    assert "LIVEKIT_API_SECRET_ARN" in env
    assert "OUTBOUND_IVR_EVENTS_TABLE" in env
    assert "OUTBOUND_IVR_ARTIFACTS_BUCKET" in env
    assert "OUTBOUND_IVR_HANDOFF_TABLE" in env
    # Monitor: always wired through outbound_ivr_stack.py (no .env/context gate any
    # more -- private-monitor-s3-spec.md), so this is always present here.
    assert "OUTBOUND_IVR_MONITOR_BUCKET" in env
    # Claimed in this test session's context (claimEvidenceNumber=true), so a
    # real (deploy-time-resolved) value is expected here, not an empty string.
    assert env["OUTBOUND_IVR_EVIDENCE_NUMBER"] != ""
    # Default context (no evidenceMode given): "connect", backward compatible.
    assert env["OUTBOUND_IVR_EVIDENCE_MODE"] == "connect"


def test_office_number_env_var_is_sourced_from_the_ssm_contract_not_context(
    outbound_ivr_template, agent_runtime_template
) -> None:
    """Through the shared `OutboundIvrStack` fixture (conftest.py), OUTBOUND_IVR_OFFICE_NUMBER
    must come from `/outbound-ivr/connect/office-number` -- the office-stack SSM
    contract outbound_ivr_stack.py reads -- not the shared fixture's own context
    `officeNumber` value (TEST_OFFICE_NUMBER), and not a FrontDesk construct
    reference (FrontDesk has no claimed-number concept any more at all; see
    test_frontdesk_stack.py). The kwarg-precedence tests above construct
    AgentRuntimeNestedStack directly and are unaffected by this -- they test
    that this stack still prefers an explicit `office_phone_number` kwarg
    over context when given one, which is exactly how outbound_ivr_stack.py's own
    SSM-sourced value reaches it."""
    ssm_params = ssm_value_parameters(outbound_ivr_template)
    (office_number_param,) = [
        lid for lid, path in ssm_params.items() if path == "/outbound-ivr/connect/office-number"
    ]
    local_name = nested_stack_local_parameter_for(
        outbound_ivr_template, "AgentRuntimeNestedStack", office_number_param
    )
    assert local_name, "AgentRuntime never receives /outbound-ivr/connect/office-number"

    env = _runtime_resource(agent_runtime_template)["Properties"]["EnvironmentVariables"]
    assert env["OUTBOUND_IVR_OFFICE_NUMBER"] == {"Ref": local_name}
    assert env["OUTBOUND_IVR_OFFICE_NUMBER"] != TEST_OFFICE_NUMBER


def test_runtime_keeps_its_direct_read_only_case_and_office_access(agent_runtime_template) -> None:
    """Unaffected by OUTBOUND_IVR_TOOLS=gateway: GatewayBackend.case_for_prompt still
    reads the case straight out of DynamoDB with this role's own GetItem (not
    through the Gateway), and the call-end path still queries/updates the case
    directly -- see agent_runtime_stack.py's ReadCaseAndOffice/
    RecordCaseOutcome statements and the design plan's shared contract. Only
    the *mutating* tool calls move to the Gateway's own (separate) Lambda
    role in gateway_stack.py."""
    statements = [
        st
        for p in agent_runtime_template.find_resources("AWS::IAM::Policy").values()
        for st in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    (read,) = [st for st in statements if st.get("Sid") == "ReadCaseAndOffice"]
    assert read["Action"] == "dynamodb:GetItem"
    assert len(read["Resource"]) == 2

    (outcome,) = [st for st in statements if st.get("Sid") == "RecordCaseOutcome"]
    assert set(outcome["Action"]) == {"dynamodb:Query", "dynamodb:UpdateItem"}


def test_execution_role_iam_matches_the_shared_contract(agent_runtime_template) -> None:
    policies = agent_runtime_template.find_resources("AWS::IAM::Policy")
    sids_to_actions = {}
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            sid = statement.get("Sid")
            if sid:
                actions = statement["Action"]
                sids_to_actions[sid] = {actions} if isinstance(actions, str) else set(actions)

    assert sids_to_actions["InvokeNovaSonic"] == {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:InvokeModelWithBidirectionalStream",
    }
    assert sids_to_actions["WriteCaseAndRecordingArtifacts"] == {"s3:PutObject"}
    assert sids_to_actions["WriteCallEvents"] == {"dynamodb:PutItem"}
    assert sids_to_actions["WriteHandoffCode"] == {"dynamodb:PutItem"}
    assert sids_to_actions["ReadLiveKitApiSecret"] == {"secretsmanager:GetSecretValue"}


def test_monitor_grants_are_absent_when_no_bucket_is_passed(stub_dirs) -> None:
    """A standalone construction with no monitor bucket (this file's own
    _standalone_agent_runtime_stack helper) must still synthesize, with no
    OUTBOUND_IVR_MONITOR_BUCKET env var and none of the monitor IAM statements --
    see agent_runtime_stack.py's own conditional."""
    template = _standalone_agent_runtime_stack(
        stub_dirs, office_phone_number=None, office_number_context="+15555550101"
    )
    env = _runtime_resource(template)["Properties"]["EnvironmentVariables"]
    assert "OUTBOUND_IVR_MONITOR_BUCKET" not in env
    statements = [
        st
        for p in template.find_resources("AWS::IAM::Policy").values()
        for st in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    monitor_sids = {
        "WriteMonitorSegments",
        "ReadMonitorWatchMarkers",
        "DeleteMonitorObjectsOnClose",
        "ListMonitorBucketForClose",
    }
    assert not [st for st in statements if st.get("Sid") in monitor_sids]


def test_monitor_grants_are_scoped_to_the_bucket_and_its_own_prefixes(
    agent_runtime_template,
) -> None:
    """Every Monitor IAM statement is scoped to the monitor bucket's own ARN
    and exactly the `seg/`/`watch/` key prefixes private-monitor-s3-spec.md's
    "Runtime IAM" lists -- never a wildcard across buckets."""
    statements = [
        st
        for p in agent_runtime_template.find_resources("AWS::IAM::Policy").values()
        for st in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    sids_to_statement = {st.get("Sid"): st for st in statements if st.get("Sid")}

    write = sids_to_statement["WriteMonitorSegments"]
    assert write["Action"] == "s3:PutObject"
    assert "/seg/*" in json.dumps(write["Resource"])

    read = sids_to_statement["ReadMonitorWatchMarkers"]
    assert read["Action"] == "s3:GetObject"
    assert "/watch/*" in json.dumps(read["Resource"])

    delete = sids_to_statement["DeleteMonitorObjectsOnClose"]
    assert delete["Action"] == "s3:DeleteObject"
    rendered = json.dumps(delete["Resource"])
    assert "/seg/*" in rendered
    assert "/watch/*" in rendered

    listing = sids_to_statement["ListMonitorBucketForClose"]
    assert listing["Action"] == "s3:ListBucket"
    resources = (
        listing["Resource"] if isinstance(listing["Resource"], list) else [listing["Resource"]]
    )
    assert len(resources) == 1
    assert "/seg/" not in json.dumps(resources) and "/watch/" not in json.dumps(resources)


def test_monitor_env_var_is_the_bucket_name(agent_runtime_template) -> None:
    env = _runtime_resource(agent_runtime_template)["Properties"]["EnvironmentVariables"]
    assert "OUTBOUND_IVR_MONITOR_BUCKET" in env
    assert env["OUTBOUND_IVR_MONITOR_BUCKET"] != ""


def test_nova_sonic_resource_is_scoped_to_one_model_arn(agent_runtime_template) -> None:
    policies = agent_runtime_template.find_resources("AWS::IAM::Policy")
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Sid") == "InvokeNovaSonic":
                resources = (
                    statement["Resource"]
                    if isinstance(statement["Resource"], list)
                    else [statement["Resource"]]
                )
                assert len(resources) == 1
                assert resources[0] == (
                    "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-5-sonic"
                )


def test_s3_write_is_scoped_to_cases_and_recordings_prefixes(agent_runtime_template) -> None:
    policies = agent_runtime_template.find_resources("AWS::IAM::Policy")
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Sid") != "WriteCaseAndRecordingArtifacts":
                continue
            resources = statement["Resource"]
            assert len(resources) == 2
            import json

            rendered = json.dumps(resources)
            assert "/cases/*" in rendered
            assert "/recordings/*" in rendered


def test_network_mode_is_vpc(agent_runtime_template) -> None:
    runtime = _runtime_resource(agent_runtime_template)
    network_config = runtime["Properties"]["NetworkConfiguration"]
    assert network_config["NetworkMode"] == "VPC"


def test_runtime_can_use_the_data_key_for_events_and_artifacts(agent_runtime_template) -> None:
    """Without it, the first event write fails with a KMS access denied error and the
    call dies before the office answers."""
    statements = [
        st
        for p in agent_runtime_template.find_resources("AWS::IAM::Policy").values()
        for st in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    (kms,) = [st for st in statements if st.get("Sid") == "UseDataKey"]
    assert {"kms:GenerateDataKey", "kms:Decrypt"} <= set(kms["Action"])


def test_runtime_role_meets_bedrocks_bidirectional_stream_requirements(
    agent_runtime_template,
) -> None:
    """Bedrock authorizes the stream as bedrock:InvokeModel, and rejects it for an
    AgentCore role whose name lacks the AmazonBedrockAgentCoreSDKRuntime- prefix."""
    (role,) = [
        r
        for name, r in agent_runtime_template.find_resources("AWS::IAM::Role").items()
        if name.startswith("AgentExecutionRole")
        and "bedrock-agentcore.amazonaws.com"
        in json.dumps(r["Properties"]["AssumeRolePolicyDocument"])
    ]
    assert role["Properties"]["RoleName"].startswith("AmazonBedrockAgentCoreSDKRuntime-")
    statements = [
        st
        for p in agent_runtime_template.find_resources("AWS::IAM::Policy").values()
        for st in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    (sonic,) = [st for st in statements if st.get("Sid") == "InvokeNovaSonic"]
    assert "bedrock:InvokeModel" in sonic["Action"]


def test_direct_evidence_mode_dials_the_cell_not_the_connect_did(app_context) -> None:
    """Direct mode must ring the person's phone; it once kept the escalation DID, so
    the call sat in the evidence queue's hold loop."""
    import aws_cdk as cdk
    from aws_cdk.assertions import Template
    from stacks.outbound_ivr_stack import OutboundIvrStack

    app = cdk.App(
        context={**app_context, "evidenceMode": "direct", "testCellNumber": "+15555550100"}
    )
    stack = OutboundIvrStack(app, "TestDirectEvidence")
    runtime_stack = next(c for c in stack.node.children if "AgentRuntime" in c.node.id)
    template = Template.from_stack(runtime_stack)
    (runtime,) = template.find_resources("AWS::BedrockAgentCore::Runtime").values()
    env = runtime["Properties"]["EnvironmentVariables"]
    assert env["OUTBOUND_IVR_EVIDENCE_MODE"] == "direct"
    assert env["OUTBOUND_IVR_EVIDENCE_NUMBER"] == "+15555550100"


def test_online_evaluation_scores_every_call_from_the_runtime(agent_runtime_template) -> None:
    (config,) = agent_runtime_template.find_resources(
        "AWS::BedrockAgentCore::OnlineEvaluationConfig"
    ).values()
    props = config["Properties"]
    assert props["Rule"]["SamplingConfig"]["SamplingPercentage"] == 100
    assert props["ExecutionStatus"] == "ENABLED"
    evaluators = {e["EvaluatorId"] for e in props["Evaluators"]}
    assert {"Builtin.GoalSuccessRate", "Builtin.ToolSelectionAccuracy"} <= evaluators


def test_call_content_goes_on_traces_unless_turned_off(agent_runtime_template, app_context) -> None:
    """On for the synthetic demo; context traceCallContent=false before real patient data."""
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    env = _runtime_resource(agent_runtime_template)["Properties"]["EnvironmentVariables"]
    assert env["OUTBOUND_IVR_TRACE_CALL_CONTENT"] == "1"
    # "true" KEEPS prompt and response text on the platform's spans
    # (omni-send-ai-agent-telemetry.html). On, it has to be present.
    assert env["AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT"] == "true"

    app = cdk.App(context={**app_context, "traceCallContent": "false"})
    stack = OutboundIvrStack(
        app, "OutboundIvrNoContent", env=cdk.Environment(account=ACCOUNT_ID, region=REGION)
    )
    nested = next(c for c in stack.node.children if c.node.id == "AgentRuntime")
    (runtime,) = (
        Template.from_stack(nested).find_resources("AWS::BedrockAgentCore::Runtime").values()
    )
    off_env = runtime["Properties"]["EnvironmentVariables"]
    assert off_env["OUTBOUND_IVR_TRACE_CALL_CONTENT"] == "0"
    # The one switch has to govern both halves. This flag was hardcoded "true"
    # regardless, so traceCallContent=false used to leave every prompt and model
    # response on the platform's spans. AWS documents no value for the other
    # direction, so off means the variable is absent, not set to "false".
    assert "AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT" not in off_env


def _agentcore_trust_statements(template, logical_id_prefix: str) -> list[dict]:
    for logical_id, resource in template.find_resources("AWS::IAM::Role").items():
        if not logical_id.startswith(logical_id_prefix):
            continue
        statements = resource["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        return [s for s in statements if "agentcore" in json.dumps(s.get("Principal", {}))]
    raise AssertionError(f"no role whose logical id starts with {logical_id_prefix}")


def test_execution_role_trust_policy_prevents_confused_deputy(agent_runtime_template) -> None:
    """A service principal with no SourceAccount/SourceArn condition lets any
    caller who can reach AgentCore in any account assume this role, which reads
    the cases table and bills Nova Sonic here. AWS documents this exact pair for
    this exact role (runtime-permissions.html).

    Asserting there is exactly one such statement matters as much as asserting the
    condition: an unconditioned statement sitting next to a conditioned one grants
    everything the conditioned one was meant to refuse.
    """
    statements = _agentcore_trust_statements(agent_runtime_template, "AgentExecutionRole")
    assert len(statements) == 1
    (statement,) = statements
    condition = statement["Condition"]
    assert condition["StringEquals"]["aws:SourceAccount"] == ACCOUNT_ID
    assert condition["ArnLike"]["aws:SourceArn"] == (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/outbound_ivr_agent*"
    ), "account-wide ':*' is broader than it needs to be; the runtime name is a literal"


def test_evaluation_sampling_defaults_to_every_call(agent_runtime_template) -> None:
    (config,) = agent_runtime_template.find_resources(
        "AWS::BedrockAgentCore::OnlineEvaluationConfig"
    ).values()
    assert config["Properties"]["Rule"]["SamplingConfig"]["SamplingPercentage"] == 100


def test_evaluation_sampling_is_a_lever(stub_dirs) -> None:
    """Six LLM judges per call is a cost trap at any volume above a demo's, and
    it used to be a literal in the stack."""
    app = cdk.App(
        context={
            **_LAMBDA_AND_POLICY_CONTEXT,
            "runtimeImageDir": str(stub_dirs["runtime_image"]),
            "dashboardDistDir": str(stub_dirs["dashboard_dist"]),
            "testCellNumber": TEST_CELL_NUMBER,
            "officeNumber": TEST_OFFICE_NUMBER,
            "agentEmail": TEST_AGENT_EMAIL,
            "evaluationSamplingPercentage": 10,
        }
    )
    stack = OutboundIvrStack(
        app, "SampledOutboundIvr", env=cdk.Environment(account=ACCOUNT_ID, region=REGION)
    )
    template = Template.from_stack(stack.agent_runtime)
    (config,) = template.find_resources("AWS::BedrockAgentCore::OnlineEvaluationConfig").values()
    assert config["Properties"]["Rule"]["SamplingConfig"]["SamplingPercentage"] == 10


def test_an_out_of_range_sampling_percentage_fails_at_synth(stub_dirs) -> None:
    app = cdk.App(
        context={
            **_LAMBDA_AND_POLICY_CONTEXT,
            "runtimeImageDir": str(stub_dirs["runtime_image"]),
            "dashboardDistDir": str(stub_dirs["dashboard_dist"]),
            "testCellNumber": TEST_CELL_NUMBER,
            "officeNumber": TEST_OFFICE_NUMBER,
            "agentEmail": TEST_AGENT_EMAIL,
            "evaluationSamplingPercentage": 0,
        }
    )
    with pytest.raises(ValueError, match="between 1 and 100"):
        OutboundIvrStack(
            app, "ZeroSampledOutboundIvr", env=cdk.Environment(account=ACCOUNT_ID, region=REGION)
        )


def test_the_sip_media_timeout_fires_before_the_app_backstop(agent_runtime_template) -> None:
    """livekit-sip must be the one that notices a dead leg, not the agent's net.

    A far-end hangup through the Voice Connector does not reliably produce a BYE the
    bridge sees, so the only thing that ends such a call is livekit-sip's no-RTP timeout
    -- and that timeout is whatever `CreateSIPParticipantRequest.media.media_timeout`
    asked for. At its 120s default a measured call left a human on a dead line for two
    minutes. Setting it low makes livekit-sip remove the participant, which reaches the
    agent as a real `participant_disconnected`.

    The ordering against the agent's own backstop is asserted from the other side, in
    tests/test_handoff_silence.py, which can import the agent; this venv cannot.
    """
    env = _runtime_resource(agent_runtime_template)["Properties"]["EnvironmentVariables"]
    sip_timeout = float(env["OUTBOUND_IVR_SIP_MEDIA_TIMEOUT_S"])
    assert 0 < sip_timeout <= 20
