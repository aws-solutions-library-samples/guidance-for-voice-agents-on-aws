"""Assertions for outbound-ivr-gateway: AWS_IAM (SigV4) inbound auth (not
Cognito/CUSTOM_JWT), one policy engine in ENFORCE mode wired onto the Gateway,
one Policy per policies/dogwood/*.dw, and the tools Lambda pointed at this
app's own cases/offices tables (not infra's outbound-ivr-legacy-cases/outbound-ivr-legacy-offices) --
see gateway_stack.py's module docstring and the design plan's shared contract.
"""

from __future__ import annotations

import glob
import json
import os

from stacks.constants import ACCOUNT_ID, REGION


def _gateway_resource(gateway_template):
    (gateway,) = gateway_template.find_resources("AWS::BedrockAgentCore::Gateway").values()
    return gateway


def test_gateway_authorizer_is_aws_iam(gateway_template) -> None:
    gateway = _gateway_resource(gateway_template)
    assert gateway["Properties"]["AuthorizerType"] == "AWS_IAM"


def test_gateway_has_no_cognito_resources(gateway_template) -> None:
    """AWS_IAM inbound auth needs no Cognito user pool, app client, domain, or
    resource server at all -- unlike a CUSTOM_JWT design would."""
    assert gateway_template.find_resources("AWS::Cognito::UserPool") == {}
    assert gateway_template.find_resources("AWS::Cognito::UserPoolClient") == {}
    assert gateway_template.find_resources("AWS::Cognito::UserPoolDomain") == {}
    assert gateway_template.find_resources("AWS::SecretsManager::Secret") == {}


def test_one_policy_engine_in_enforce_mode(gateway_template) -> None:
    gateway_template.resource_count_is("AWS::BedrockAgentCore::PolicyEngine", 1)
    gateway = _gateway_resource(gateway_template)
    policy_engine_config = gateway["Properties"]["PolicyEngineConfiguration"]
    assert policy_engine_config["Mode"] == "ENFORCE"
    assert "Arn" in policy_engine_config


def test_one_policy_per_dogwood_statement_file(gateway_template) -> None:
    dogwood_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "infrastructure", "policies", "dogwood"
    )
    statement_files = glob.glob(os.path.join(dogwood_dir, "*.dw"))
    assert len(statement_files) == 5, "sanity check: policies/dogwood/*.dw moved?"

    policies = gateway_template.find_resources("AWS::BedrockAgentCore::Policy")
    assert len(policies) == 5
    for policy in policies.values():
        assert policy["Properties"]["Definition"]["Policy"]["Statement"], (
            "every policy must carry the .dw file's real statement text"
        )


def test_policy_statements_reference_the_gateway_arn(gateway_template) -> None:
    _gateway_resource(gateway_template)  # exactly one gateway resource exists
    policies = gateway_template.find_resources("AWS::BedrockAgentCore::Policy")
    for policy in policies.values():
        statement = policy["Properties"]["Definition"]["Policy"]["Statement"]
        # ${GATEWAY_ARN} is substituted at synth time with a CDK token
        # (Fn::GetAtt on the Gateway) -- it must not survive verbatim.
        assert "${GATEWAY_ARN}" not in statement


def test_tools_lambda_env_points_at_this_apps_own_tables(gateway_template) -> None:
    """lambdas/tools/handler.py's own defaults (OUTBOUND_IVR_CASES_TABLE=outbound-ivr-cases) must
    not be relied on as-is -- set explicitly here so this doesn't depend on
    handler.py's own fallback happening to match this deployment's table (it
    doesn't match infra's own outbound-ivr-legacy-cases/outbound-ivr-legacy-offices either)."""
    (fn,) = gateway_template.find_resources("AWS::Lambda::Function").values()
    env_vars = fn["Properties"]["Environment"]["Variables"]
    assert "OUTBOUND_IVR_CASES_TABLE" in env_vars
    assert "OUTBOUND_IVR_OFFICES_TABLE" in env_vars


def test_tools_lambda_is_not_vpc_attached(gateway_template) -> None:
    """The Gateway invokes its Lambda target over lambda:InvokeFunction IAM,
    not network reachability -- no VpcConfig needed (see the module
    docstring's "Tools Lambda is not VPC-attached" section)."""
    (fn,) = gateway_template.find_resources("AWS::Lambda::Function").values()
    assert "VpcConfig" not in fn["Properties"]


def test_tools_lambda_role_has_no_vpc_managed_policy(gateway_template) -> None:
    roles = gateway_template.find_resources("AWS::IAM::Role")
    (tools_role,) = [
        r
        for r in roles.values()
        if "lambda.amazonaws.com" in str(r["Properties"]["AssumeRolePolicyDocument"])
    ]
    managed = tools_role["Properties"].get("ManagedPolicyArns", [])
    assert managed == [], "no AWSLambdaVPCAccessExecutionRole needed off-VPC"


def test_gateway_target_name_matches_the_policy_action_prefix(gateway_template) -> None:
    """policies/dogwood/*.dw reference actions like
    AgentCore::Action::"OutboundIvrTools___record_item" -- the Gateway target's name
    must stay "OutboundIvrTools" or every policy silently stops matching."""
    (target,) = gateway_template.find_resources("AWS::BedrockAgentCore::GatewayTarget").values()
    assert target["Properties"]["Name"] == "OutboundIvrTools"


def _policy_engine_access(gateway_template):
    policies = gateway_template.find_resources("AWS::IAM::Policy")
    (access,) = [p for name, p in policies.items() if name.startswith("GatewayPolicyEngineAccess")]
    return access


def test_gateway_role_can_evaluate_its_policy_engine(gateway_template) -> None:
    """CreateGateway calls GetPolicyEngine with the Gateway's own role; without
    these the Gateway fails to create (Access denied while calling GetPolicyEngine)."""
    access = _policy_engine_access(gateway_template)
    actions = set()
    for statement in access["Properties"]["PolicyDocument"]["Statement"]:
        listed = statement["Action"]
        actions.update([listed] if isinstance(listed, str) else listed)
    assert {
        "bedrock-agentcore:GetPolicyEngine",
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
        "bedrock-agentcore:GetWorkloadAccessToken",
    } <= actions


def test_gateway_waits_for_its_policy_engine_access(gateway_template) -> None:
    gateway = _gateway_resource(gateway_template)
    policies = gateway_template.find_resources("AWS::IAM::Policy")
    (access_id,) = [name for name in policies if name.startswith("GatewayPolicyEngineAccess")]
    assert access_id in gateway.get("DependsOn", [])


def test_target_tool_schema_is_a_bare_array_of_tool_definitions() -> None:
    """CreateGatewayTarget rejects the {"_comment", "tools"} wrapper ("The lambda
    target configuration in S3 does not match expected format")."""
    import json

    from stacks.gateway_stack import _stage_tool_definitions

    here = os.path.dirname(os.path.abspath(__file__))
    staged = _stage_tool_definitions(os.path.join(here, "../../src/lambdas/tools/schema.json"))
    with open(staged, encoding="utf-8") as handle:
        tools = json.load(handle)
    assert isinstance(tools, list)
    assert {t["name"] for t in tools} == {"record_item", "escalate", "complete", "transfer_to_team"}
    assert all({"name", "description", "inputSchema"} <= set(t) for t in tools)


def test_backend_calls_tools_by_the_gateways_prefixed_name() -> None:
    """The Gateway lists tools as "<target>___<tool>"; Outbound IVR, the policies and the
    Lambda all have to agree with the target this stack creates."""
    import re

    from stacks.gateway_stack import GATEWAY_TARGET_NAME

    here = os.path.dirname(os.path.abspath(__file__))
    with open(
        os.path.join(here, "../../src/outbound_ivr/voice/backend.py"), encoding="utf-8"
    ) as handle:
        (prefix,) = re.findall(r'^GATEWAY_TOOL_PREFIX = "([^"]+)"', handle.read(), re.MULTILINE)
    assert prefix == f"{GATEWAY_TARGET_NAME}___"


def test_forbid_policies_wait_for_every_permit(gateway_template) -> None:
    """A forbid created while its action has no permit yet is rejected as Overly
    Restrictive (it would deny every transfer_to_team request)."""
    policies = gateway_template.find_resources("AWS::BedrockAgentCore::Policy")
    (once_id,) = [name for name in policies if name.startswith("PolicyTransferToTeamOnce")]
    permit_ids = {name for name in policies if name != once_id}
    assert permit_ids <= set(policies[once_id].get("DependsOn", []))


def test_tools_lambda_role_cannot_read_the_offices_table(gateway_template) -> None:
    """The four tools only touch the case item; least privilege."""
    import json

    policies = gateway_template.find_resources("AWS::IAM::Policy")
    tools = [p for name, p in policies.items() if name.startswith("ToolsLambdaRole")]
    assert tools and "OfficesTable" not in json.dumps(tools)


def test_gateway_service_role_trust_is_conditioned_in_place(gateway_template) -> None:
    """The Gateway L2 construct, left alone, emits TWO sts:AssumeRole statements
    for bedrock-agentcore: one scoped and one unconditioned, and the unconditioned
    one grants everything the scoped one refuses.

    The document is overridden on the construct's OWN role rather than replacing
    the role. Swapping roles fails to deploy: it moves GatewayPolicyEngineAccess
    off the old role while the Gateway's update still assumes it for the
    policy-engine association, which then 403s and rolls the stack back. The
    exactly-one assertion is the point of this test.
    """
    roles = {
        logical_id: resource
        for logical_id, resource in gateway_template.find_resources("AWS::IAM::Role").items()
        if logical_id.startswith("ToolsGatewayServiceRole")
    }
    assert roles, "the Gateway's own construct-generated role must still be the one in use"
    (resource,) = roles.values()
    statements = [
        s
        for s in resource["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        if "agentcore" in json.dumps(s.get("Principal", {}))
    ]
    assert len(statements) == 1, "an unconditioned statement would defeat the conditioned one"
    condition = statements[0]["Condition"]
    assert condition["StringEquals"]["aws:SourceAccount"] == ACCOUNT_ID
    assert condition["ArnLike"]["aws:SourceArn"] == (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:gateway/outbound-ivr-tools*"
    ), "this is what the construct's own defeated statement scoped to; do not widen it"
