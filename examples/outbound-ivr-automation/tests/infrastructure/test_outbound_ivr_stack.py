"""Assertions for OutboundIvrStack: one top-level stack, ten nested children, each a
real `AWS::CloudFormation::Stack` resource in the parent template. The
office-stack split moves the mock provider office (and its one Connect
instance) out of this stack entirely, into the independent `OutboundIvrOfficeStack`
(`stacks/office_stack.py`) -- this file's central job is proving that split
actually holds: OutboundIvrStack creates zero Connect instances anywhere in its own
nested tree, never imports or constructs `OutboundIvrOfficeStack`, and learns
everything it needs about the office through the SSM contract that stack
publishes (`ssm.StringParameter.from_string_parameter_name(...).string_value`,
a deploy-time CloudFormation dynamic reference -- never a construct
reference, and never `value_from_lookup`, which would need synth-time AWS
credentials this suite does not have). FrontDesk keeps being built
unconditionally -- see outbound_ivr_stack.py's own module docstring.
"""

from __future__ import annotations

import ast
import inspect

from aws_cdk.assertions import Template

from infrastructure.conftest import nested_stack_local_parameter_for, ssm_value_parameters

# OutboundIvrOfficeStack's own SSM contract -- the full four paths it writes
# unconditionally, every deploy (see test_outbound_ivr_office_stack.py).
OFFICE_SSM_CONTRACT_PATHS = {
    "/outbound-ivr/connect/instance-id",
    "/outbound-ivr/connect/instance-arn",
    "/outbound-ivr/connect/office-number",
    "/outbound-ivr/connect/front-desk-queue-arn",
}

# Of those four, the ones OutboundIvrStack itself actually has a reader for:
# front-desk-queue-arn's real readers are connect/make_flow.py and
# scripts/deploy_flow.sh, both outside this CDK app -- outbound_ivr_stack.py
# deliberately does not read it too, since an SSM-backed template parameter
# this stack never referenced would just be dead template weight (see its own
# module docstring's comment on that parameter).
OFFICE_SSM_PATHS_OUTBOUND_IVR_STACK_CONSUMES = OFFICE_SSM_CONTRACT_PATHS - {
    "/outbound-ivr/connect/front-desk-queue-arn"
}


def test_ten_nested_stacks(outbound_ivr_template) -> None:
    nested = outbound_ivr_template.find_resources("AWS::CloudFormation::Stack")
    assert len(nested) == 10, f"expected 10 nested stacks, found {len(nested)}: {list(nested)}"


def test_front_desk_is_always_built_with_no_connect_instance_id_context(
    stacks, app_context
) -> None:
    """The old `connectInstanceId` gate (and its Annotations.add_warning) is
    gone: FrontDesk is built every time now, learning the office's instance
    id from the SSM contract rather than a borrowed/constructed instance."""
    assert "connectInstanceId" not in app_context
    assert "connectInstanceAlias" not in app_context
    assert stacks["front_desk"] is not None
    assert stacks["outbound_ivr"].front_desk is stacks["front_desk"]


def test_no_top_level_duplicate_of_the_old_four_stacks(stacks) -> None:
    """The old four independent top-level stacks (OutboundIvrNetwork,
    OutboundIvrLiveKit, OutboundIvrAgentCoreTest, OutboundIvrEscalation) are gone -- everything
    this app itself owns lives under the one OutboundIvrStack top-level stack now
    (OutboundIvrOfficeStack is a deliberate, separate top-level stack for a
    different reason -- see its own module docstring -- but it is never built
    by, or a child of, OutboundIvrStack, which is what this test actually checks)."""
    app = stacks["app"]
    top_level_stack_ids = {
        child.node.id for child in app.node.children if hasattr(child, "template_options")
    }
    assert top_level_stack_ids == {"TestOutboundIvrStack"}


def test_each_nested_stack_is_a_real_nestedstack_construct(stacks) -> None:
    from aws_cdk import NestedStack

    for name in (
        "network",
        "livekit",
        "escalation",
        "data",
        "gateway",
        "agent_runtime",
        "dashboard",
        "front_desk",
        "ops",
    ):
        assert isinstance(stacks[name], NestedStack), f"{name} is not a NestedStack"


def test_agent_runtime_depends_on_its_six_prerequisites(outbound_ivr_template) -> None:
    """CloudFormation must create network/livekit/escalation/data/Monitor/gateway
    before the Outbound IVR Runtime (the Gateway's own MCP URL goes into the Runtime's
    env -- see agent_runtime_stack.py; the Monitor tables too, for
    src/outbound_ivr/voice/monitor.py's MonitorPublisher), and the Runtime before the
    dashboard (its Lambda needs the Runtime's ARN) -- outbound_ivr_stack.py's explicit
    add_dependency() calls, each rendered as a DependsOn on the nested stack's
    own AWS::CloudFormation::Stack resource. None of these six is the office
    any more -- that relationship is an SSM parameter read, never a
    CloudFormation dependency (see the SSM-contract tests below)."""
    nested = outbound_ivr_template.find_resources("AWS::CloudFormation::Stack")

    def depends_on(prefix: str) -> set[str]:
        resource = next(r for lid, r in nested.items() if lid.startswith(prefix))
        return set(resource.get("DependsOn") or [])

    agent_runtime_deps = depends_on("AgentRuntimeNestedStack")
    assert len(agent_runtime_deps) == 6
    assert any(d.startswith("GatewayNestedStack") for d in agent_runtime_deps)
    assert any(d.startswith("MonitorNestedStack") for d in agent_runtime_deps)

    dashboard_deps = depends_on("DashboardNestedStack")
    assert any(d.startswith("AgentRuntimeNestedStack") for d in dashboard_deps)
    assert any(d.startswith("DataNestedStack") for d in dashboard_deps)
    assert any(d.startswith("MonitorNestedStack") for d in dashboard_deps)

    livekit_deps = depends_on("LiveKitNestedStack")
    assert any(d.startswith("NetworkNestedStack") for d in livekit_deps)

    gateway_deps = depends_on("GatewayNestedStack")
    assert any(d.startswith("DataNestedStack") for d in gateway_deps)

    monitor_deps = depends_on("MonitorNestedStack")
    assert any(d.startswith("DataNestedStack") for d in monitor_deps)

    # Escalation no longer has anything to explicitly depend on: it used to
    # wait on the (now-deleted) ConnectInstanceNestedStack for instance_arn;
    # that value is an SSM-sourced token now, which carries no CloudFormation
    # ordering requirement at all.
    assert depends_on("EscalationNestedStack") == set()

    # Front desk likewise no longer waits on a Connect-instance nested stack
    # -- only on Dashboard, for its own CloudFront domain (the approved-origin
    # association's real target).
    front_desk_deps = depends_on("FrontDeskNestedStack")
    assert any(d.startswith("DashboardNestedStack") for d in front_desk_deps), (
        "front desk's approved-origin association needs the dashboard's own CloudFront domain"
    )
    assert not any("ConnectInstance" in d for d in front_desk_deps)


def test_outbound_ivr_stack_contains_zero_connect_instances(stacks) -> None:
    """The thing the office-stack split exists for: OutboundIvrStack must never
    create a Connect instance anywhere in its own nested tree again --
    OutboundIvrOfficeStack owns the only one (see test_outbound_ivr_office_stack.py's
    `test_exactly_one_connect_instance`)."""
    owners = []
    for name, nested in stacks.items():
        if name in ("app", "outbound_ivr"):
            continue
        count = len(Template.from_stack(nested).find_resources("AWS::Connect::Instance"))
        if count:
            owners.append((name, count))
    assert owners == [], f"OutboundIvrStack must create zero Connect instances; found: {owners}"


def test_outbound_ivr_stack_has_no_construct_level_reference_to_the_office_stack(stacks) -> None:
    """OutboundIvrStack and OutboundIvrOfficeStack are wired together only through the SSM
    contract (paths, not constructs) -- outbound_ivr_stack.py must never import or
    instantiate OutboundIvrOfficeStack, and nothing in OutboundIvrStack's own construct
    tree may be one. Checked via the AST, not a raw substring search: the
    module docstring and comments legitimately *talk about*
    `OutboundIvrOfficeStack` in prose (explaining the split) without that being a
    real construct-level reference -- only an actual import or call counts."""
    from stacks import outbound_ivr_stack as outbound_ivr_stack_module

    tree = ast.parse(inspect.getsource(outbound_ivr_stack_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "stacks.office_stack" and node.module != "office_stack", (
                f"outbound_ivr_stack.py must not import from office_stack: {ast.dump(node)}"
            )
            assert all(alias.name != "OutboundIvrOfficeStack" for alias in node.names), (
                "outbound_ivr_stack.py must not import OutboundIvrOfficeStack by name"
            )
        elif isinstance(node, ast.Import):
            assert all("office_stack" not in alias.name for alias in node.names), (
                f"outbound_ivr_stack.py must not import office_stack: {ast.dump(node)}"
            )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "OutboundIvrOfficeStack", (
                "outbound_ivr_stack.py must not construct OutboundIvrOfficeStack"
            )

    outbound_ivr = stacks["outbound_ivr"]
    for child in outbound_ivr.node.find_all():
        assert type(child).__name__ != "OutboundIvrOfficeStack"


def test_outbound_ivr_stack_reads_the_office_ssm_contract_not_a_stack_dependency(
    outbound_ivr_template,
) -> None:
    """outbound_ivr_stack.py's own replacement for the old ConnectInstanceNestedStack
    cross-reference: every value it needs about the office comes from
    `ssm.StringParameter.from_string_parameter_name(self, "<id>", "<path>")
    .string_value` -- a CloudFormation dynamic reference resolved at *deploy*
    time (never `value_from_lookup`, which would need real AWS credentials at
    *synth* time -- this suite has none, and these fixtures still build)."""
    ssm_params = ssm_value_parameters(outbound_ivr_template)
    paths = set(ssm_params.values())
    # Exactly the three OutboundIvrStack has a reader for -- not all four (the
    # fourth, front-desk-queue-arn, is deliberately unread here; see this
    # file's own module-level comment on OFFICE_SSM_PATHS_OUTBOUND_IVR_STACK_CONSUMES)
    # and not some other path entirely.
    assert paths == OFFICE_SSM_PATHS_OUTBOUND_IVR_STACK_CONSUMES, (
        f"expected exactly {OFFICE_SSM_PATHS_OUTBOUND_IVR_STACK_CONSUMES}, got: {paths}"
    )


def test_front_desk_and_escalation_receive_the_ssm_backed_instance_identifier(
    outbound_ivr_template,
) -> None:
    """Checked at the parent template's own nested-stack Parameters (how CDK
    actually passes a parent-scoped token into a nested stack's constructor),
    without assuming any particular auto-generated parameter name -- the
    mirror image of the pre-split `test_connect_instance_arn_flows_into_
    escalation_and_front_desk`, which checked an Fn::GetAtt cross-nested-stack
    reference that no longer exists."""
    ssm_params = ssm_value_parameters(outbound_ivr_template)
    (instance_id_param,) = [
        lid for lid, path in ssm_params.items() if path == "/outbound-ivr/connect/instance-id"
    ]
    (instance_arn_param,) = [
        lid for lid, path in ssm_params.items() if path == "/outbound-ivr/connect/instance-arn"
    ]

    front_desk_local = nested_stack_local_parameter_for(
        outbound_ivr_template, "FrontDeskNestedStack", instance_id_param
    )
    assert front_desk_local, "FrontDesk never receives /outbound-ivr/connect/instance-id"

    escalation_local = nested_stack_local_parameter_for(
        outbound_ivr_template, "EscalationNestedStack", instance_arn_param
    )
    assert escalation_local, "Escalation never receives /outbound-ivr/connect/instance-arn"


# --------------------------------------------------------------------------
# Alarms. There were none: the review confirmed zero CloudWatch alarms, zero
# SNS topics and zero budgets live in the account, so a dead LiveKit task or a
# failing tools Lambda first showed up as a call that did not work.
# --------------------------------------------------------------------------


def _ops_template(stacks):
    return Template.from_stack(stacks["ops"])


def test_every_alarm_notifies_the_topic(stacks) -> None:
    template = _ops_template(stacks)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert alarms, "the ops stack must create alarms"
    (topic_id,) = template.find_resources("AWS::SNS::Topic").keys()
    for logical_id, alarm in alarms.items():
        actions = alarm["Properties"].get("AlarmActions") or []
        assert actions, f"{logical_id} has no alarm action, so it tells nobody"
        assert any({"Ref": topic_id} == a for a in actions), (
            f"{logical_id} does not notify the alert topic"
        )


def test_the_media_plane_alarms_treat_no_data_as_broken(stacks) -> None:
    """A task count that stops reporting is the failure, not the absence of one."""
    template = _ops_template(stacks)
    by_name = {
        a["Properties"]["AlarmName"]: a["Properties"]
        for a in template.find_resources("AWS::CloudWatch::Alarm").values()
    }
    for name in ("outbound-ivr-livekit-task-gone", "outbound-ivr-livekit-host-gone"):
        assert by_name[name]["TreatMissingData"] == "breaching"
        assert by_name[name]["ComparisonOperator"] == "LessThanThreshold"
        assert by_name[name]["Threshold"] == 1


def test_the_hot_path_lambdas_and_tables_are_covered(stacks) -> None:
    template = _ops_template(stacks)
    names = {
        a["Properties"]["AlarmName"]
        for a in template.find_resources("AWS::CloudWatch::Alarm").values()
    }
    assert {
        "outbound-ivr-tools-errors",
        "outbound-ivr-lookup-errors",
        "outbound-ivr-api-errors",
        "outbound-ivr-cases-throttled",
        "outbound-ivr-events-throttled",
    } <= names


def test_the_topic_is_encrypted_and_tls_only(stacks) -> None:
    template = _ops_template(stacks)
    (topic,) = template.find_resources("AWS::SNS::Topic").values()
    assert topic["Properties"].get("KmsMasterKeyId"), "the alert topic must be encrypted"
    (policy,) = template.find_resources("AWS::SNS::TopicPolicy").values()
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    assert any(
        s.get("Effect") == "Deny"
        and s.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
        for s in statements
    )


def test_the_topic_lets_cloudwatch_publish_to_it(stacks) -> None:
    """Creating ANY explicit topic policy replaces the broad default SNS gives a
    new topic, so without an Allow the first alarm to fire fails its publish with
    an authorization error visible only in that alarm's History tab, and
    -c alertEmail delivers nothing."""
    template = _ops_template(stacks)
    (policy,) = template.find_resources("AWS::SNS::TopicPolicy").values()
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    allows = [
        s
        for s in statements
        if s.get("Effect") == "Allow"
        and s.get("Principal", {}).get("Service") == "cloudwatch.amazonaws.com"
    ]
    assert allows, "no principal is allowed to publish, so no alarm can notify"
    (allow,) = allows
    assert "sns:Publish" in (
        allow["Action"] if isinstance(allow["Action"], list) else [allow["Action"]]
    )
    assert allow["Condition"]["StringEquals"]["aws:SourceAccount"]


def test_the_host_alarm_has_a_metric_to_read(stacks) -> None:
    """GroupInServiceInstances is only published if the ASG enables group metrics.
    Without that the alarm, treating missing data as breaching, has no data point
    it could ever read as healthy and sits in ALARM from two minutes after every
    deploy onward."""
    livekit = Template.from_stack(stacks["livekit"])
    (asg,) = livekit.find_resources("AWS::AutoScaling::AutoScalingGroup").values()
    collections = asg["Properties"].get("MetricsCollection")
    assert collections, "the ASG publishes no group metrics at all"
    assert any(c.get("Granularity") == "1Minute" for c in collections)


def test_alert_email_context_subscribes_the_address(stub_dirs) -> None:
    """Without this context value the alarms notify nobody, which is the state the
    account was in. scripts/deploy.sh passes it from .env ALERT_EMAIL."""
    import aws_cdk as cdk
    from stacks.constants import ACCOUNT_ID, REGION
    from stacks.outbound_ivr_stack import OutboundIvrStack

    from infrastructure.conftest import (
        EXAMPLE_ROOT,
        TEST_AGENT_EMAIL,
        TEST_CELL_NUMBER,
        TEST_OFFICE_NUMBER,
    )

    app = cdk.App(
        context={
            "runtimeImageDir": str(stub_dirs["runtime_image"]),
            "dashboardDistDir": str(stub_dirs["dashboard_dist"]),
            "toolsLambdaDir": str(EXAMPLE_ROOT / "src" / "lambdas" / "tools"),
            "apiLambdaDir": str(EXAMPLE_ROOT / "src" / "lambdas" / "api"),
            "outboundIvrPackageDir": str(EXAMPLE_ROOT / "src" / "outbound_ivr"),
            "policiesDir": str(EXAMPLE_ROOT / "infrastructure" / "policies"),
            "testCellNumber": TEST_CELL_NUMBER,
            "officeNumber": TEST_OFFICE_NUMBER,
            "agentEmail": TEST_AGENT_EMAIL,
            "alertEmail": "oncall@example.com",
        }
    )
    stack = OutboundIvrStack(
        app, "AlertingOutboundIvr", env=cdk.Environment(account=ACCOUNT_ID, region=REGION)
    )
    template = Template.from_stack(stack.ops)
    (subscription,) = template.find_resources("AWS::SNS::Subscription").values()
    assert subscription["Properties"]["Protocol"] == "email"
    assert subscription["Properties"]["Endpoint"] == "oncall@example.com"


def test_no_alert_email_means_no_subscription(stacks) -> None:
    """The shared fixture leaves alertEmail unset; the topic and alarms still exist."""
    template = _ops_template(stacks)
    assert not template.find_resources("AWS::SNS::Subscription")
    assert template.find_resources("AWS::SNS::Topic")
    assert template.find_resources("AWS::CloudWatch::Alarm")
