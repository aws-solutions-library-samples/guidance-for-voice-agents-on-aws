"""Assertions for outbound-ivr-escalation: a *shared* Connect instance
(`instance_arn`, taken as a constructor argument -- escalation_stack.py does not
create its own, see its module docstring) rather than a dedicated one, the handoff
Lambda wired up so Connect can actually invoke it, the queue / routing profile /
security profile / agent user shape, no password baked into the agent user (the user
sets their own at first login), the three flows' JSON is well-formed with no dangling
action references, the claimed DID is associated with the handoff flow, and
everything taggable carries the project tag.
"""

from __future__ import annotations

import json

from infrastructure.conftest import (
    ALLOWED_CONNECT_FLOW_ACTIONS,
    TEST_AGENT_EMAIL,
    assert_flow_has_no_dangling_references,
    parse_flow_content,
)


def test_escalation_creates_no_connect_instance_of_its_own(escalation_template) -> None:
    """The Connect instance is office_stack.py's, shared with frontdesk_stack.py --
    escalation_stack.py only ever takes its arn."""
    assert escalation_template.find_resources("AWS::Connect::Instance") == {}


def test_every_connect_resource_here_is_wired_to_the_same_shared_instance_arn(
    escalation_template,
) -> None:
    """Every `connect.CfnXxx(..., instance_arn=instance_arn)` call in this stack
    must use the exact same constructor argument -- not re-derive, re-guess, or
    build its own instance. Checked by self-consistency
    (every value is identical) rather than against a literal, since the real
    value is a cross-nested-stack reference to office_stack.py's own
    output, not a plain string."""
    seen = set()
    for resource_type in (
        "AWS::Connect::Queue",
        "AWS::Connect::HoursOfOperation",
        "AWS::Connect::RoutingProfile",
        "AWS::Connect::SecurityProfile",
        "AWS::Connect::User",
        "AWS::Connect::ContactFlow",
    ):
        for resource in escalation_template.find_resources(resource_type).values():
            seen.add(json.dumps(resource["Properties"]["InstanceArn"], sort_keys=True))
    (integration,) = escalation_template.find_resources(
        "AWS::Connect::IntegrationAssociation"
    ).values()
    seen.add(json.dumps(integration["Properties"]["InstanceId"], sort_keys=True))
    assert len(seen) == 1, f"escalation's Connect resources disagree on the instance: {seen}"


def test_handoff_table_is_pay_per_request_with_ttl_and_pitr(escalation_template) -> None:
    (table,) = escalation_template.find_resources("AWS::DynamoDB::Table").values()
    props = table["Properties"]
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    (key,) = props["KeySchema"]
    assert key == {"AttributeName": "code", "KeyType": "HASH"}
    assert props["TimeToLiveSpecification"] == {"AttributeName": "expires_at", "Enabled": True}
    assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True


def test_lambda_reads_the_handoff_table_and_connect_can_invoke_it(escalation_template) -> None:
    functions = escalation_template.find_resources("AWS::Lambda::Function")
    lookup_fns = {
        k: v
        for k, v in functions.items()
        if v["Properties"].get("Handler") == "handler.lambda_handler"
    }
    assert len(lookup_fns) == 1
    (lookup_fn,) = lookup_fns.values()
    env = lookup_fn["Properties"]["Environment"]["Variables"]
    assert "HANDOFF_TABLE" in env

    (permission,) = escalation_template.find_resources("AWS::Lambda::Permission").values()
    perm_props = permission["Properties"]
    assert perm_props["Action"] == "lambda:InvokeFunction"
    assert perm_props["Principal"] == "connect.amazonaws.com"
    assert "SourceArn" in perm_props, "must be scoped to this instance, not every Connect instance"

    (integration,) = escalation_template.find_resources(
        "AWS::Connect::IntegrationAssociation"
    ).values()
    assert integration["Properties"]["IntegrationType"] == "LAMBDA_FUNCTION"


def test_lookup_lambda_can_claim_a_code_but_not_write_or_delete_items(
    escalation_template,
) -> None:
    """The lookup marks a code used with one conditional UpdateItem; it never needs
    PutItem or DeleteItem on the handoff table."""
    actions: set[str] = set()
    for policy in escalation_template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            stmt_actions = statement["Action"]
            stmt_actions = [stmt_actions] if isinstance(stmt_actions, str) else stmt_actions
            actions.update(a for a in stmt_actions if a.startswith("dynamodb:"))
    assert "dynamodb:UpdateItem" in actions
    assert "dynamodb:GetItem" in actions
    assert not actions & {"dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"}


def test_queue_and_hours_of_operation(escalation_template) -> None:
    (queue,) = escalation_template.find_resources("AWS::Connect::Queue").values()
    assert queue["Properties"]["Name"] == "Evidence team"
    assert "HoursOfOperationArn" in queue["Properties"]

    (hours,) = escalation_template.find_resources("AWS::Connect::HoursOfOperation").values()
    config = hours["Properties"]["Config"]
    assert {c["Day"] for c in config} == {
        "MONDAY",
        "TUESDAY",
        "WEDNESDAY",
        "THURSDAY",
        "FRIDAY",
        "SATURDAY",
        "SUNDAY",
    }
    for day_config in config:
        assert day_config["StartTime"] == {"Hours": 0, "Minutes": 0}
        assert day_config["EndTime"] == {"Hours": 23, "Minutes": 59}


def test_routing_profile_points_at_the_evidence_queue(escalation_template) -> None:
    (queue_id,) = escalation_template.find_resources("AWS::Connect::Queue").keys()
    (profile,) = escalation_template.find_resources("AWS::Connect::RoutingProfile").values()
    props = profile["Properties"]
    (concurrency,) = props["MediaConcurrencies"]
    assert concurrency == {"Channel": "VOICE", "Concurrency": 1}
    (queue_config,) = props["QueueConfigs"]
    assert queue_config["QueueReference"]["Channel"] == "VOICE"
    assert queue_config["QueueReference"]["QueueArn"] == {"Fn::GetAtt": [queue_id, "QueueArn"]}
    assert props["DefaultOutboundQueueArn"] == {"Fn::GetAtt": [queue_id, "QueueArn"]}


def test_security_profile_is_basic_agent_access_only(escalation_template) -> None:
    (profile,) = escalation_template.find_resources("AWS::Connect::SecurityProfile").values()
    assert profile["Properties"]["Permissions"] == ["BasicAgentAccess"]


def test_agent_user_password_is_a_secrets_manager_reference_and_uses_the_context_email(
    escalation_template,
) -> None:
    (user,) = escalation_template.find_resources("AWS::Connect::User").values()
    props = user["Properties"]
    # Connect requires a password on a CONNECT_MANAGED instance; it must be a
    # Secrets Manager dynamic reference, never a literal in the template.
    password = json.dumps(props["Password"])
    assert "{{resolve:secretsmanager:" in password
    (secret_id,) = escalation_template.find_resources("AWS::SecretsManager::Secret").keys()
    assert {"Ref": secret_id} in props["Password"]["Fn::Join"][1]
    assert props["IdentityInfo"]["Email"] == TEST_AGENT_EMAIL
    assert props["PhoneConfig"]["PhoneType"] == "SOFT_PHONE"
    assert "RoutingProfileArn" in props
    assert len(props["SecurityProfileArns"]) == 1


def test_three_flows_have_the_right_types_and_well_formed_content(escalation_template) -> None:
    flows = escalation_template.find_resources("AWS::Connect::ContactFlow")
    by_type: dict[str, list[dict]] = {}
    for resource in flows.values():
        by_type.setdefault(resource["Properties"]["Type"], []).append(resource)
    assert set(by_type) == {"CONTACT_FLOW", "CUSTOMER_QUEUE", "AGENT_WHISPER"}
    for resources in by_type.values():
        assert len(resources) == 1

    for resource in flows.values():
        flow = parse_flow_content(resource)
        assert flow["Version"] == "2019-10-30"
        assert_flow_has_no_dangling_references(flow)

    (handoff,) = by_type["CONTACT_FLOW"]
    handoff_flow = parse_flow_content(handoff)
    action_types = [a["Type"] for a in handoff_flow["Actions"]]
    for expected in (
        "GetParticipantInput",
        "InvokeLambdaFunction",
        "UpdateContactAttributes",
        "UpdateContactEventHooks",
        "UpdateContactTargetQueue",
        "TransferContactToQueue",
    ):
        assert expected in action_types, f"handoff flow missing a {expected} action"

    (whisper,) = by_type["AGENT_WHISPER"]
    whisper_flow = parse_flow_content(whisper)
    whisper_text = json.dumps(whisper_flow)
    assert "$.Attributes.summary" in whisper_text
    assert "$.Attributes.case_id" in whisper_text


def test_an_unrecognized_handoff_code_never_reaches_the_queue(escalation_template) -> None:
    """The flow must act on the lookup's `found` answer.

    Without this gate the Lambda's found "false" was ignored and any six digits
    dialed into a public DID reached a human, just with empty case attributes. The
    assertions walk the actual action graph rather than checking a Compare exists,
    because an unwired Compare would pass that weaker check.
    """
    (handoff,) = escalation_template.find_resources(
        "AWS::Connect::ContactFlow", {"Properties": {"Type": "CONTACT_FLOW"}}
    ).values()
    flow = parse_flow_content(handoff)
    by_id = {a["Identifier"]: a for a in flow["Actions"]}

    (lookup,) = [a for a in flow["Actions"] if a["Type"] == "InvokeLambdaFunction"]
    gate = by_id[lookup["Transitions"]["NextAction"]]
    assert gate["Type"] == "Compare", (
        "the lookup must hand off to a Compare, not straight to the transfer"
    )
    assert gate["Parameters"]["ComparisonValue"] == "$.External.found"

    # The only way through is found == "true".
    (passing,) = gate["Transitions"]["Conditions"]
    assert passing["Condition"] == {"Operator": "Equals", "Operands": ["true"]}
    assert by_id[passing["NextAction"]]["Type"] == "UpdateContactAttributes"

    # Every other path out of the gate dead-ends at a disconnect without ever
    # touching the queue actions.
    queue_action_types = {
        "UpdateContactEventHooks",
        "UpdateContactTargetQueue",
        "TransferContactToQueue",
    }
    deny_targets = {gate["Transitions"]["NextAction"]} | {
        e["NextAction"] for e in gate["Transitions"]["Errors"]
    }
    assert {e["ErrorType"] for e in gate["Transitions"]["Errors"]} == {"NoMatchingCondition"}
    for start in deny_targets:
        seen: set[str] = set()
        frontier = [start]
        while frontier:
            action_id = frontier.pop()
            if action_id in seen:
                continue
            seen.add(action_id)
            action = by_id[action_id]
            assert action["Type"] not in queue_action_types, (
                f"an unrecognized code can still reach {action['Type']}"
            )
            transitions = action["Transitions"]
            frontier.extend([transitions["NextAction"]] if transitions.get("NextAction") else [])
            frontier.extend(e["NextAction"] for e in transitions.get("Errors", []))
            frontier.extend(c["NextAction"] for c in transitions.get("Conditions", []))
        assert any(by_id[a]["Type"] == "DisconnectParticipant" for a in seen), (
            "the deny path must end in a disconnect"
        )


def test_a_lookup_failure_still_fails_open_to_a_human(escalation_template) -> None:
    """Deliberate asymmetry: an unrecognized code is refused, but the Lambda not
    answering at all is not the caller's fault and still reaches a person."""
    (handoff,) = escalation_template.find_resources(
        "AWS::Connect::ContactFlow", {"Properties": {"Type": "CONTACT_FLOW"}}
    ).values()
    flow = parse_flow_content(handoff)
    by_id = {a["Identifier"]: a for a in flow["Actions"]}

    (lookup,) = [a for a in flow["Actions"] if a["Type"] == "InvokeLambdaFunction"]
    (error_branch,) = lookup["Transitions"]["Errors"]
    assert error_branch["ErrorType"] == "NoMatchingError"
    message = by_id[error_branch["NextAction"]]
    assert message["Type"] == "MessageParticipant"
    assert by_id[message["Transitions"]["NextAction"]]["Type"] == "UpdateContactEventHooks"


def test_claimed_did_is_associated_with_the_handoff_flow(escalation_template) -> None:
    escalation_template.resource_count_is("AWS::Connect::PhoneNumber", 1)
    (phone,) = escalation_template.find_resources("AWS::Connect::PhoneNumber").values()
    assert phone["Properties"]["Type"] == "DID"
    assert phone["Properties"]["CountryCode"] == "US"

    (handoff_id,) = escalation_template.find_resources(
        "AWS::Connect::ContactFlow", {"Properties": {"Type": "CONTACT_FLOW"}}
    ).keys()
    (association,) = escalation_template.find_resources(
        "Custom::ConnectPhoneNumberContactFlowAssociation"
    ).values()
    # Check the raw Fn::Join parts for the actual Fn::GetAtt, not a flattened
    # string: flattening replaces every intrinsic with the same bare "<token>"
    # placeholder, which would make this assertion pass against *any* flow.
    parts = association["Properties"]["Create"]["Fn::Join"][1]
    assert {"Fn::GetAtt": [handoff_id, "ContactFlowArn"]} in parts, (
        "the association must reference the handoff flow's own ARN, not a different flow"
    )


def test_everything_taggable_carries_the_project_tag(escalation_template) -> None:
    # No AWS::Connect::Instance here: escalation_stack.py never creates one --
    # see test_outbound_ivr_office_stack.py for that resource's tag coverage (the
    # instance lives in OutboundIvrOfficeStack now).
    tag_bearing_types = (
        "AWS::Connect::Queue",
        "AWS::Connect::RoutingProfile",
        "AWS::Connect::SecurityProfile",
        "AWS::Connect::User",
        "AWS::Connect::HoursOfOperation",
        "AWS::Connect::ContactFlow",
        "AWS::Connect::PhoneNumber",
        "AWS::Connect::IntegrationAssociation",
        "AWS::DynamoDB::Table",
        "AWS::Lambda::Function",
    )
    for resource_type in tag_bearing_types:
        for resource in escalation_template.find_resources(resource_type).values():
            tags = {t["Key"]: t["Value"] for t in resource["Properties"].get("Tags", [])}
            assert tags.get("project") == "outbound-ivr", f"{resource_type} missing project tag"


def test_every_flow_uses_only_action_types_its_flow_type_allows(escalation_template) -> None:
    for resource in escalation_template.find_resources("AWS::Connect::ContactFlow").values():
        flow_type = resource["Properties"]["Type"]
        used = {a["Type"] for a in parse_flow_content(resource)["Actions"]}
        allowed = ALLOWED_CONNECT_FLOW_ACTIONS[flow_type]
        assert used <= allowed, f"{flow_type}: not allowed {used - allowed}"


def test_did_flow_association_unlinks_on_delete(escalation_template) -> None:
    """Connect refuses to delete a flow a phone number still points at, so the
    association must disassociate on delete, before the flow goes."""
    (association,) = escalation_template.find_resources(
        "Custom::ConnectPhoneNumberContactFlowAssociation"
    ).values()
    assert "disassociatePhoneNumberContactFlow" in json.dumps(association["Properties"]["Delete"])


def test_connect_grants_are_scoped_to_named_resources(escalation_template) -> None:
    """The custom resource behind the evidence DID's flow association gets its Connect actions on
    specific ARNs, never Resource "*": every one of these actions supports
    resource-level permissions (Service Authorization Reference, Connect)."""
    from infrastructure.conftest import connect_statements

    statements = connect_statements(escalation_template)
    assert statements, "expected a Connect grant for the custom resource"
    for statement in statements:
        resources = statement["Resource"]
        resources = [resources] if not isinstance(resources, list) else resources
        assert "*" not in resources, statement
