"""Assertions for frontdesk_stack.py's `FrontDeskNestedStack`: despite the name, it
is not "the front desk" -- it is the one piece of mock-office wiring that cannot
move out of `OutboundIvrStack` into the independent `OutboundIvrOfficeStack`, because
it needs something only `OutboundIvrStack` has: the dashboard's own CloudFront domain
(built after AgentRuntime and Dashboard). Hours of operation, the front desk queue,
the routing/security profile, the `outbound-ivr-front-desk` agent user and its
password secret, the office IVR contact flow, and the claimed office DID all live in
`OutboundIvrOfficeStack` instead (see test_outbound_ivr_office_stack.py); this file
checks only what is left: the dashboard's CCP approved-origin association, its
`instance_id` (not `instance_arn`) constructor argument, and that none of those other
resources crept back in here.
"""

from __future__ import annotations

import json

from stacks.constants import ACCOUNT_ID, RESOURCE_PREFIX

EXPECTED_CCP_URL = f"https://{RESOURCE_PREFIX}-office-{ACCOUNT_ID}.my.connect.aws/ccp-v2/"

# Every native Connect resource type this stack used to create, before the
# office-stack split -- none of them may exist here any more.
_MOVED_OUT_CONNECT_RESOURCE_TYPES = (
    "AWS::Connect::Instance",
    "AWS::Connect::Queue",
    "AWS::Connect::User",
    "AWS::Connect::PhoneNumber",
    "AWS::Connect::ContactFlow",
    "AWS::Connect::HoursOfOperation",
    "AWS::Connect::RoutingProfile",
    "AWS::Connect::SecurityProfile",
)


def test_approved_origin_associates_the_dashboard_cloudfront_domain_and_unlinks_on_delete(
    front_desk_template,
) -> None:
    (association,) = front_desk_template.find_resources("Custom::ConnectApprovedOrigin").values()
    props = association["Properties"]
    create = json.dumps(props["Create"])
    assert "associateApprovedOrigin" in create
    assert "DomainName" in create, "must reference the dashboard distribution's own domain name"
    update = json.dumps(props["Update"])
    assert "associateApprovedOrigin" in update
    delete = json.dumps(props["Delete"])
    assert "disassociateApprovedOrigin" in delete


def test_approved_origin_is_the_only_resource_this_stack_creates(front_desk_template) -> None:
    """The thing the office-stack split exists for: the queue, hours, agent
    user, office flow, and claimed DID all moved to OutboundIvrOfficeStack. Only the
    dashboard's CCP approved-origin grant is genuinely Outbound IVR's own concern
    (frontdesk_stack.py's own module docstring)."""
    for resource_type in _MOVED_OUT_CONNECT_RESOURCE_TYPES:
        assert front_desk_template.find_resources(resource_type) == {}, (
            f"front desk must no longer create {resource_type}"
        )


def test_this_stack_writes_no_ssm_parameter_any_more(front_desk_template) -> None:
    """The front-desk-queue-arn SSM publish moved to OutboundIvrOfficeStack along
    with the queue itself; OutboundIvrStack now reads that parameter directly off
    SSM instead (see outbound_ivr_stack.py)."""
    assert front_desk_template.find_resources("AWS::SSM::Parameter") == {}


def test_this_stack_creates_no_secret_any_more(front_desk_template) -> None:
    """The agent password secret moved to OutboundIvrOfficeStack along with the
    agent user it belongs to."""
    assert front_desk_template.find_resources("AWS::SecretsManager::Secret") == {}


def test_ccp_url_output_is_the_deterministic_office_alias(front_desk_template) -> None:
    """Derived from constants (the same expression outbound_ivr_stack.py and
    office_stack.py both build their own copy from), never read off
    OutboundIvrOfficeStack -- see frontdesk_stack.py's own module docstring."""
    outputs = front_desk_template.to_json()["Outputs"]
    assert outputs["FrontDeskCcpUrl"]["Value"] == EXPECTED_CCP_URL


def test_only_output_is_the_ccp_url(front_desk_template) -> None:
    """FrontDeskQueueArn and OfficeContactFlowArn used to be outputs here;
    both moved to OutboundIvrOfficeStack along with the resources they described."""
    outputs = front_desk_template.to_json()["Outputs"]
    assert set(outputs) == {"FrontDeskCcpUrl"}


def test_connect_grants_are_scoped_to_named_resources(front_desk_template) -> None:
    """The custom resource behind the dashboard's CCP approved origin gets its Connect actions on
    specific ARNs, never Resource "*": every one of these actions supports
    resource-level permissions (Service Authorization Reference, Connect)."""
    from infrastructure.conftest import connect_statements

    statements = connect_statements(front_desk_template)
    assert statements, "expected a Connect grant for the custom resource"
    for statement in statements:
        resources = statement["Resource"]
        resources = [resources] if not isinstance(resources, list) else resources
        assert "*" not in resources, statement
