"""Assertions for office_stack.py's `OutboundIvrOfficeStack`: a standalone, top-level
`Stack` (never nested under `OutboundIvrStack`) that owns the mock provider office
end to end -- the Connect instance, hours of operation, the front-desk queue,
the security/routing profiles, the `outbound-ivr-front-desk` agent user and its
generated password secret, the office IVR contact flow, and the optional
claimed office DID. `claimOfficeNumber` defaults TRUE here (unlike every other
claim flag in this app), so the shared `office_stack`/`office_template`
fixtures below exercise the claimed path by default; `unclaimed_office_stack`/
`unclaimed_office_template` exercise `-c claimOfficeNumber=false` explicitly.

Also exercises the SSM contract this stack publishes unconditionally, every
deploy -- `/outbound-ivr/connect/instance-id`, `-instance-arn`, `-office-number`, and
`-front-desk-queue-arn` -- and that a flow file missing its placeholder fails synth
loudly rather than silently deploying an unresolved one.

Built standalone, under its own throwaway `cdk.App()`, never through the
shared `OutboundIvrStack` session fixture in conftest.py -- this stack is deployed
completely independently of `OutboundIvrStack` (see its own module docstring), so
these tests stay correct regardless of how `outbound_ivr_stack.py` reads the SSM
parameters this file checks; that reading is asserted separately, in
test_outbound_ivr_stack.py.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk import NestedStack, Stack
from aws_cdk.assertions import Template
from stacks.constants import ACCOUNT_ID, PROJECT_TAG, REGION, RESOURCE_PREFIX
from stacks.office_stack import (
    FRONT_DESK_QUEUE_ARN_PARAMETER,
    INSTANCE_ARN_PARAMETER,
    INSTANCE_ID_PARAMETER,
    OFFICE_FLOW_QUEUE_ARN_PLACEHOLDER,
    OFFICE_NUMBER_PARAMETER,
    OFFICE_NUMBER_PLACEHOLDER,
    OutboundIvrOfficeStack,
)

from infrastructure.conftest import (
    ALLOWED_CONNECT_FLOW_ACTIONS,
    OFFICE_FLOW_PATH,
    TEST_AGENT_EMAIL,
    assert_flow_has_no_dangling_references,
    flatten_token_string,
    parse_flow_content,
)

EXPECTED_ALIAS = f"{RESOURCE_PREFIX}-office-{ACCOUNT_ID}"

CONTRACT_PARAMETER_PATHS = {
    "/outbound-ivr/connect/instance-id",
    "/outbound-ivr/connect/instance-arn",
    "/outbound-ivr/connect/office-number",
    "/outbound-ivr/connect/front-desk-queue-arn",
}


def _build(
    context: dict | None = None, *, construct_id: str = "TestOutboundIvrOffice"
) -> OutboundIvrOfficeStack:
    base_context = {"agentEmail": TEST_AGENT_EMAIL, "contactFlowPath": str(OFFICE_FLOW_PATH)}
    app = cdk.App(context={**base_context, **(context or {})})
    env = cdk.Environment(account=ACCOUNT_ID, region=REGION)
    return OutboundIvrOfficeStack(app, construct_id, env=env)


@pytest.fixture(scope="module")
def office_stack() -> OutboundIvrOfficeStack:
    """The default build: `claimOfficeNumber` is left unset, so the stack's own
    TRUE default applies and a DID is claimed -- unlike every other
    claim-a-phone-number flag in this app, which defaults False."""
    return _build()


@pytest.fixture(scope="module")
def office_template(office_stack) -> Template:
    return Template.from_stack(office_stack)


@pytest.fixture(scope="module")
def unclaimed_office_stack() -> OutboundIvrOfficeStack:
    return _build({"claimOfficeNumber": "false"}, construct_id="TestUnclaimedOffice")


@pytest.fixture(scope="module")
def unclaimed_office_template(unclaimed_office_stack) -> Template:
    return Template.from_stack(unclaimed_office_stack)


# --------------------------------------------------------------------------
# It's a standalone top-level Stack, not a NestedStack -- the whole point of
# this move (office_stack.py's own module docstring: "not a NestedStack, and
# not a child of OutboundIvrStack").
# --------------------------------------------------------------------------


def test_this_stack_is_a_plain_top_level_stack_not_a_nested_stack(office_stack) -> None:
    assert isinstance(office_stack, Stack)
    assert not isinstance(office_stack, NestedStack)


# --------------------------------------------------------------------------
# The Connect instance itself -- exactly one, CONNECT_MANAGED, inbound-only,
# with a globally-unique alias that carries the account id.
# --------------------------------------------------------------------------


def test_exactly_one_connect_instance(office_template) -> None:
    office_template.resource_count_is("AWS::Connect::Instance", 1)


def test_instance_is_connect_managed_and_inbound_only(office_template) -> None:
    (instance,) = office_template.find_resources("AWS::Connect::Instance").values()
    props = instance["Properties"]
    assert props["IdentityManagementType"] == "CONNECT_MANAGED"
    assert props["Attributes"]["InboundCalls"] is True
    assert props["Attributes"]["OutboundCalls"] is False
    assert props["Attributes"]["ContactflowLogs"] is True


def test_alias_is_exactly_the_documented_deterministic_string(office_template) -> None:
    (instance,) = office_template.find_resources("AWS::Connect::Instance").values()
    assert instance["Properties"]["InstanceAlias"] == EXPECTED_ALIAS


def test_alias_carries_the_account_id(office_template) -> None:
    """A Connect instance alias is globally unique across every AWS account,
    not just this one, and becomes the CCP domain (<alias>.my.connect.aws) --
    without the account id, a plain "outbound-ivr-office" could collide with a wholly
    different account's own instance."""
    (instance,) = office_template.find_resources("AWS::Connect::Instance").values()
    assert ACCOUNT_ID in instance["Properties"]["InstanceAlias"]


def test_instance_arn_and_id_attributes_are_exposed_and_distinct(office_stack) -> None:
    assert office_stack.instance_alias == EXPECTED_ALIAS
    assert office_stack.instance_arn
    assert office_stack.instance_id
    resolved_arn = Stack.of(office_stack).resolve(office_stack.instance_arn)
    resolved_id = Stack.of(office_stack).resolve(office_stack.instance_id)
    assert resolved_arn != resolved_id


# --------------------------------------------------------------------------
# Hours, queue, routing/security profile, agent user -- every Connect
# resource in this stack must hang off the one instance just created.
# --------------------------------------------------------------------------


def test_every_connect_resource_shares_the_one_instance_arn(office_template) -> None:
    seen = set()
    for resource_type in (
        "AWS::Connect::Queue",
        "AWS::Connect::HoursOfOperation",
        "AWS::Connect::RoutingProfile",
        "AWS::Connect::SecurityProfile",
        "AWS::Connect::User",
        "AWS::Connect::ContactFlow",
    ):
        for resource in office_template.find_resources(resource_type).values():
            seen.add(json.dumps(resource["Properties"]["InstanceArn"], sort_keys=True))
    assert len(seen) == 1, f"office stack's Connect resources disagree on the instance: {seen}"


def test_queue_hours_and_routing_profile(office_template) -> None:
    (queue,) = office_template.find_resources("AWS::Connect::Queue").values()
    # The agent whisper speaks this name to whoever answers.
    assert queue["Properties"]["Name"] == "Prior authorization desk"

    (hours,) = office_template.find_resources("AWS::Connect::HoursOfOperation").values()
    assert hours["Properties"]["Name"] == "outbound-ivr-front-desk-always-open"
    config = hours["Properties"]["Config"]
    assert len(config) == 7
    for day_config in config:
        assert day_config["StartTime"] == {"Hours": 0, "Minutes": 0}
        assert day_config["EndTime"] == {"Hours": 23, "Minutes": 59}

    (queue_id,) = office_template.find_resources("AWS::Connect::Queue").keys()
    (profile,) = office_template.find_resources("AWS::Connect::RoutingProfile").values()
    props = profile["Properties"]
    assert props["Name"] == "outbound-ivr-front-desk-routing"
    (concurrency,) = props["MediaConcurrencies"]
    assert concurrency == {"Channel": "VOICE", "Concurrency": 1}
    (queue_config,) = props["QueueConfigs"]
    assert queue_config["QueueReference"]["QueueArn"] == {"Fn::GetAtt": [queue_id, "QueueArn"]}


def test_security_profile_is_basic_agent_access_only(office_template) -> None:
    (profile,) = office_template.find_resources("AWS::Connect::SecurityProfile").values()
    assert profile["Properties"]["SecurityProfileName"] == "outbound-ivr-front-desk-agent"
    assert profile["Properties"]["Permissions"] == ["BasicAgentAccess"]


def test_agent_user_password_is_a_secrets_manager_reference(office_template) -> None:
    (user,) = office_template.find_resources("AWS::Connect::User").values()
    props = user["Properties"]
    assert props["Username"] == "outbound-ivr-front-desk"
    password = json.dumps(props["Password"])
    assert "{{resolve:secretsmanager:" in password
    (secret_id,) = office_template.find_resources("AWS::SecretsManager::Secret").keys()
    assert {"Ref": secret_id} in props["Password"]["Fn::Join"][1]
    assert props["IdentityInfo"]["Email"] == TEST_AGENT_EMAIL
    assert props["PhoneConfig"] == {"PhoneType": "SOFT_PHONE", "AutoAccept": False}

    (secret,) = office_template.find_resources("AWS::SecretsManager::Secret").values()
    assert secret["Properties"]["Name"] == "outbound-ivr/front-desk-agent-password"


def test_missing_agent_email_fails_at_synth() -> None:
    app = cdk.App(context={"contactFlowPath": str(OFFICE_FLOW_PATH)})
    env = cdk.Environment(account=ACCOUNT_ID, region=REGION)
    with pytest.raises(ValueError, match="agentEmail"):
        OutboundIvrOfficeStack(app, "NoEmailOffice", env=env)


# --------------------------------------------------------------------------
# The office IVR contact flow: the committed placeholder must be substituted,
# and a flow file missing it must fail synth loudly rather than deploy a
# literal, unresolvable "${...}".
# --------------------------------------------------------------------------


def test_office_flow_is_well_formed_with_no_unsubstituted_placeholder(
    office_stack, office_template
) -> None:
    (flow,) = office_template.find_resources(
        "AWS::Connect::ContactFlow", {"Properties": {"Type": "CONTACT_FLOW"}}
    ).values()
    assert flow["Properties"]["Name"] == "outbound-ivr-office-mock-provider-ivr"

    flattened = flatten_token_string(flow["Properties"]["Content"])
    assert "${" not in flattened, (
        "the office flow still carries an unsubstituted ${...} placeholder"
    )

    parsed = parse_flow_content(flow)
    assert parsed["Version"] == "2019-10-30"
    assert_flow_has_no_dangling_references(parsed)
    used_actions = {a["Type"] for a in parsed["Actions"]}
    allowed = ALLOWED_CONNECT_FLOW_ACTIONS["CONTACT_FLOW"]
    assert used_actions <= allowed, f"not allowed in a CONTACT_FLOW: {used_actions - allowed}"
    # The real transfer target is the queue's own token, not a leftover
    # placeholder -- check it landed in the right action rather than
    # comparing text.
    (set_queue,) = [a for a in parsed["Actions"] if a["Type"] == "UpdateContactTargetQueue"]
    assert set_queue["Parameters"]["QueueId"] == "<token>"
    assert any(a["Type"] == "TransferContactToQueue" for a in parsed["Actions"])

    assert office_stack.office_flow_arn


def test_flow_missing_the_placeholder_fails_synth_loudly(tmp_path) -> None:
    bad_flow = tmp_path / "no-placeholder.json"
    bad_flow.write_text(json.dumps({"Version": "2019-10-30", "StartAction": "a", "Actions": []}))
    with pytest.raises(ValueError, match="FRONT_DESK_QUEUE_ARN"):
        _build({"contactFlowPath": str(bad_flow)}, construct_id="BadFlowOffice")


def test_the_committed_placeholder_constant_is_what_the_flow_file_actually_carries() -> None:
    """Self-consistency guard: if connect/make_flow.py's own placeholder
    constant ever drifts from this module's copy of it, the check above would
    stop catching anything."""
    flow_text = OFFICE_FLOW_PATH.read_text(encoding="utf-8")
    assert OFFICE_FLOW_QUEUE_ARN_PLACEHOLDER in flow_text


# --------------------------------------------------------------------------
# The claimed office DID. claimOfficeNumber defaults TRUE in this stack
# (unlike every other claim flag in this app), so `office_stack`/
# `office_template` above already exercise the claimed path; this block
# checks the association's own shape and the documented opt-out.
# --------------------------------------------------------------------------


def test_office_number_is_claimed_by_default(office_stack) -> None:
    assert office_stack.office_phone_number is not None
    assert office_stack.office_number == office_stack.office_phone_number
    assert office_stack.office_number != OFFICE_NUMBER_PLACEHOLDER


def test_claimed_did_creates_exactly_one_phone_number(office_template) -> None:
    office_template.resource_count_is("AWS::Connect::PhoneNumber", 1)
    (phone,) = office_template.find_resources("AWS::Connect::PhoneNumber").values()
    assert phone["Properties"]["Type"] == "DID"
    assert phone["Properties"]["CountryCode"] == "US"


def test_claimed_did_is_associated_with_the_office_flow(office_template) -> None:
    (flow_id,) = office_template.find_resources(
        "AWS::Connect::ContactFlow", {"Properties": {"Type": "CONTACT_FLOW"}}
    ).keys()
    (association,) = office_template.find_resources(
        "Custom::ConnectPhoneNumberContactFlowAssociation"
    ).values()
    # Check the raw Fn::Join parts for the actual Fn::GetAtt, not a flattened
    # string: flattening replaces every intrinsic with the same bare "<token>"
    # placeholder, which would make this assertion pass against *any* flow.
    parts = association["Properties"]["Create"]["Fn::Join"][1]
    assert {"Fn::GetAtt": [flow_id, "ContactFlowArn"]} in parts, (
        "the association must reference the office flow's own ARN, not a different flow"
    )


def test_claimed_did_association_unlinks_on_delete(office_template) -> None:
    """Connect refuses to delete a flow a phone number still points at."""
    (association,) = office_template.find_resources(
        "Custom::ConnectPhoneNumberContactFlowAssociation"
    ).values()
    assert "disassociatePhoneNumberContactFlow" in json.dumps(association["Properties"]["Delete"])


def test_claim_office_number_false_synthesizes_with_no_did(
    unclaimed_office_stack, unclaimed_office_template
) -> None:
    assert unclaimed_office_stack.office_phone_number is None
    assert unclaimed_office_template.find_resources("AWS::Connect::PhoneNumber") == {}
    assert (
        unclaimed_office_template.find_resources("Custom::ConnectPhoneNumberContactFlowAssociation")
        == {}
    )


@pytest.mark.parametrize("flag_value", ["false", "0", "no", False])
def test_claim_office_number_accepts_falsey_spellings(flag_value) -> None:
    stack = _build({"claimOfficeNumber": flag_value}, construct_id=f"FalseyOffice{flag_value}")
    assert stack.office_phone_number is None


@pytest.mark.parametrize("flag_value", ["true", "1", "yes", True])
def test_claim_office_number_accepts_truthy_spellings(flag_value) -> None:
    stack = _build({"claimOfficeNumber": flag_value}, construct_id=f"TruthyOffice{flag_value}")
    assert stack.office_phone_number is not None


def test_claim_office_number_rejects_an_unparseable_value() -> None:
    with pytest.raises(ValueError, match="claimOfficeNumber"):
        _build({"claimOfficeNumber": "maybe"}, construct_id="UnparseableOffice")


# --------------------------------------------------------------------------
# The SSM contract: all four parameters, exact contract paths, every deploy
# -- including office-number when no DID is claimed. This is the piece that
# actually kills the two-pass deploy, so it is checked thoroughly.
# --------------------------------------------------------------------------


def test_all_four_ssm_parameters_exist_with_exact_contract_paths(office_template) -> None:
    names = {
        p["Properties"]["Name"]
        for p in office_template.find_resources("AWS::SSM::Parameter").values()
    }
    assert names == CONTRACT_PARAMETER_PATHS

    # The module's own constants must actually be these exact literal paths --
    # catches a drift between the constant and the string it is meant to hold.
    assert INSTANCE_ID_PARAMETER == "/outbound-ivr/connect/instance-id"
    assert INSTANCE_ARN_PARAMETER == "/outbound-ivr/connect/instance-arn"
    assert OFFICE_NUMBER_PARAMETER == "/outbound-ivr/connect/office-number"
    assert FRONT_DESK_QUEUE_ARN_PARAMETER == "/outbound-ivr/connect/front-desk-queue-arn"


def test_ssm_parameter_values_match_the_resources_they_publish(
    office_stack, office_template
) -> None:
    params = {
        p["Properties"]["Name"]: p["Properties"]["Value"]
        for p in office_template.find_resources("AWS::SSM::Parameter").values()
    }
    resolve = Stack.of(office_stack).resolve

    assert params["/outbound-ivr/connect/instance-id"] == resolve(office_stack.instance_id)
    assert params["/outbound-ivr/connect/instance-arn"] == resolve(office_stack.instance_arn)
    assert (
        params["/outbound-ivr/connect/instance-id"] != params["/outbound-ivr/connect/instance-arn"]
    ), "the id and arn parameters must not both resolve to the same attribute"

    (queue_id,) = office_template.find_resources("AWS::Connect::Queue").keys()
    assert params["/outbound-ivr/connect/front-desk-queue-arn"] == {
        "Fn::GetAtt": [queue_id, "QueueArn"]
    }

    # Claimed by this fixture's default, so this must be the real claimed DID,
    # never the placeholder.
    assert params["/outbound-ivr/connect/office-number"] == resolve(
        office_stack.office_phone_number
    )
    assert params["/outbound-ivr/connect/office-number"] != OFFICE_NUMBER_PLACEHOLDER


def test_office_number_parameter_is_written_even_when_claim_office_number_is_false(
    unclaimed_office_stack, unclaimed_office_template
) -> None:
    """The one assertion that would actually hurt if wrong: a reader
    (OutboundIvrStack) must never see this parameter simply not exist. Writing it
    conditionally would make OutboundIvrStack's own SSM read fail at deploy -- see
    this stack's own module docstring."""
    names_to_params = {
        p["Properties"]["Name"]: p
        for p in unclaimed_office_template.find_resources("AWS::SSM::Parameter").values()
    }
    assert names_to_params.keys() == CONTRACT_PARAMETER_PATHS, (
        "all four parameters must exist, not three"
    )

    office_number_param = names_to_params["/outbound-ivr/connect/office-number"]
    assert office_number_param["Properties"]["Value"] == OFFICE_NUMBER_PLACEHOLDER
    assert unclaimed_office_stack.office_number == OFFICE_NUMBER_PLACEHOLDER


def test_placeholder_is_the_documented_synthetic_e164_number() -> None:
    assert OFFICE_NUMBER_PLACEHOLDER == "+15555550101"


# --------------------------------------------------------------------------
# Outputs, tags, and the "nothing else Connect-shaped snuck in" guard.
# --------------------------------------------------------------------------


def test_outputs_exist(office_template) -> None:
    outputs = office_template.to_json()["Outputs"]
    for name in (
        "ConnectInstanceId",
        "ConnectInstanceAlias",
        "OfficePhoneNumberOutput",
        "FrontDeskQueueArn",
        "OfficeContactFlowArn",
        "FrontDeskAgentPasswordSecretName",
    ):
        assert name in outputs, f"expected a CfnOutput named {name!r}"


def test_everything_taggable_carries_the_project_tag(office_template) -> None:
    tag_bearing_types = (
        "AWS::Connect::Queue",
        "AWS::Connect::RoutingProfile",
        "AWS::Connect::SecurityProfile",
        "AWS::Connect::User",
        "AWS::Connect::HoursOfOperation",
        "AWS::Connect::ContactFlow",
        "AWS::SecretsManager::Secret",
    )
    for resource_type in tag_bearing_types:
        for resource in office_template.find_resources(resource_type).values():
            tags = {t["Key"]: t["Value"] for t in resource["Properties"].get("Tags", [])}
            assert tags.get("project") == PROJECT_TAG, f"{resource_type} missing project tag"


def test_connect_resource_types_are_exactly_the_documented_set(office_template) -> None:
    """Guards against "simplifying" by adding some unrelated Connect resource
    here, or forgetting to remove one if this stack's shape ever shrinks."""
    template_json = office_template.to_json()
    connect_resource_types = {
        r["Type"]
        for r in template_json.get("Resources", {}).values()
        if r["Type"].startswith("AWS::Connect::")
    }
    assert connect_resource_types == {
        "AWS::Connect::Instance",
        "AWS::Connect::HoursOfOperation",
        "AWS::Connect::Queue",
        "AWS::Connect::RoutingProfile",
        "AWS::Connect::SecurityProfile",
        "AWS::Connect::User",
        "AWS::Connect::ContactFlow",
        "AWS::Connect::PhoneNumber",
    }


def test_connect_grants_are_scoped_to_named_resources(office_template) -> None:
    """The custom resource behind the office DID's flow association gets its Connect actions on
    specific ARNs, never Resource "*": every one of these actions supports
    resource-level permissions (Service Authorization Reference, Connect)."""
    from infrastructure.conftest import connect_statements

    statements = connect_statements(office_template)
    assert statements, "expected a Connect grant for the custom resource"
    for statement in statements:
        resources = statement["Resource"]
        resources = [resources] if not isinstance(resources, list) else resources
        assert "*" not in resources, statement
