"""outbound-ivr-gateway: the tools Gateway (record_item / escalate / complete /
transfer_to_team), backed by a Lambda target, with every call checked by an
AgentCore Policy engine, pointed at this app's own deployed tables
(`outbound-ivr-cases`/`outbound-ivr-offices`, passed in from `data_stack.py` via
`outbound_ivr_stack.py`) with `AWS_IAM` (SigV4) inbound auth:

- `AWS_IAM` is a real, named authorizer type (`CUSTOM_JWT`/`AWS_IAM`/`NONE`), not
  `NONE` -- the same reasoning `agent_runtime_stack.py`'s own Runtime already
  relies on for its default (IAM/SigV4) inbound authorizer.
- Outbound IVR's Runtime signs Gateway calls itself with its own ambient execution-role
  credentials (botocore SigV4, service `bedrock-agentcore`); no Cognito user
  pool, app client, or client-secret placeholder is needed at all. See
  `agent_runtime_stack.py` for the `grant_invoke`/env wiring.

**Tools Lambda is not VPC-attached.** The Gateway invokes a Lambda target over
plain `lambda:InvokeFunction` IAM, not network reachability into any VPC (per
AWS's own Gateway-prerequisites documentation) -- so there is no Gateway-side
reason to attach it. Keeping it off the VPC also matches this app's own
`dashboard_stack.py` API Lambda, which reaches the same tables the same way
(default Lambda networking, no VPC, no `AWSLambdaVPCAccessExecutionRole`).

**Policy engine.** One `CfnPolicyEngine` plus one `CfnPolicy` per file under
`policies_dir` (Dogwood `.dw` by default, `-c policyLanguage=cedar` for the plain
Cedar statements), `PolicyEngineConfiguration: {Mode: ENFORCE}` property-overridden
onto the Gateway's underlying `CfnGateway`. None of the five Dogwood statements
reference `principal.hasTag(...)` or a JWT claim: switching the authorizer type to
`AWS_IAM` only changes the principal's *type* (`AgentCore::IamEntity::"<arn>"` vs
`AgentCore::OAuthUser::"<sub>"`), which none of these statements test.
"""

from __future__ import annotations

import glob
import json
import os
import re
import tempfile

from aws_cdk import Duration, NestedStack, RemovalPolicy, Tags
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from cdk_nag import NagSuppressions
from constructs import Construct

from .constants import PROJECT_TAG, RESOURCE_PREFIX

# The Gateway target's name becomes the Cedar/Dogwood action-name prefix
# ("<TargetName>___<toolName>", e.g. "OutboundIvrTools___record_item") -- must match
# ../policies/dogwood/*.dw, which reference this exact prefix.
GATEWAY_TARGET_NAME = "OutboundIvrTools"
GATEWAY_NAME = f"{RESOURCE_PREFIX}-tools"


class GatewayNestedStack(NestedStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cases_table: dynamodb.Table,
        offices_table: dynamodb.Table,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        tools_lambda_dir = self.node.try_get_context("toolsLambdaDir") or "../src/lambdas/tools"
        outbound_ivr_package_dir = (
            self.node.try_get_context("outboundIvrPackageDir") or "../src/outbound_ivr"
        )
        tool_schema_path = self.node.try_get_context("toolSchemaPath") or os.path.join(
            tools_lambda_dir, "schema.json"
        )
        policies_dir = self.node.try_get_context("policiesDir") or "policies"
        policy_language = (self.node.try_get_context("policyLanguage") or "dogwood").lower()
        if policy_language not in ("dogwood", "cedar"):
            raise ValueError(
                f"policyLanguage must be 'dogwood' or 'cedar', got {policy_language!r}"
            )

        # ------------------------------------------------------------------
        # Tools Lambda: record_item / escalate / complete / transfer_to_team,
        # reading/writing this app's own cases/offices tables (not infra's
        # outbound-ivr-cases/outbound-ivr-offices -- those table names are the wrong ones
        # for this deployment, see the shared contract).
        # ------------------------------------------------------------------
        tools_lambda_role = iam.Role(
            self,
            "ToolsLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        # The four tools only touch the case item (outbound_ivr.tools.business), never offices.
        cases_table.grant_read_write_data(tools_lambda_role)
        NagSuppressions.add_resource_suppressions(
            tools_lambda_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "grant_read_write_data()/grant_read_data() add each "
                        "table's own GSI wildcard ('.../index/*', since a Query "
                        "against the status GSI is a real access pattern -- see "
                        "lambdas/tools/handler.py's dispatch) and the shared "
                        "CMK's kms:GenerateDataKey*/kms:ReEncrypt* actions, "
                        "which have no resource-level permissions narrower than "
                        "the key itself. Both are the standard CDK grant shape "
                        "for an encrypted table, not a wildcard this stack "
                        "chose to add."
                    ),
                    "appliesTo": [
                        {"regex": "/^Resource::<CasesTable.*\\.Arn>\\/index\\/\\*$/"},
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
                    ],
                },
            ],
            apply_to_children=True,
        )

        tools_lambda_asset_dir = _build_tools_lambda_asset(
            tools_lambda_dir, outbound_ivr_package_dir, extra_files=("schema.json",)
        )
        # The role has no AWSLambdaBasicExecutionRole, so without this the
        # Lambda runs but writes no logs anywhere.
        tools_log_group = logs.LogGroup(
            self,
            "ToolsLambdaLogGroup",
            log_group_name=f"/aws/lambda/{RESOURCE_PREFIX}-tools",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        tools_log_group.grant_write(tools_lambda_role)
        tools_lambda = _lambda.Function(
            self,
            "ToolsLambda",
            function_name=f"{RESOURCE_PREFIX}-tools",
            runtime=_lambda.Runtime.PYTHON_3_14,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(tools_lambda_asset_dir),
            role=tools_lambda_role,
            log_group=tools_log_group,
            # This Lambda runs mid-conversation: the caller is on the line waiting
            # while it writes. The dashboard's API Lambda, which only serves a
            # browser poll, had already been measured and raised to 512MB while
            # this one sat at the 128MB default, which is backwards. More memory
            # also means more CPU, which is what actually shortens the cold start.
            memory_size=512,
            timeout=Duration.seconds(10),
            environment={
                "OUTBOUND_IVR_CASES_TABLE": cases_table.table_name,
                "OUTBOUND_IVR_OFFICES_TABLE": offices_table.table_name,
            },
        )

        # ------------------------------------------------------------------
        # Gateway: AWS_IAM (SigV4) inbound only -- no Cognito user pool,
        # resource server, app client, or client-secret placeholder (see
        # this module's docstring).
        # ------------------------------------------------------------------
        gateway_authorizer = agentcore.GatewayAuthorizer.using_aws_iam()

        gateway = agentcore.Gateway(
            self,
            "ToolsGateway",
            gateway_name=GATEWAY_NAME,
            description="Outbound IVR's evidence and escalation tools, policy-checked (AWS_IAM inbound)",
            authorizer_configuration=gateway_authorizer,
        )

        # ------------------------------------------------------------------
        # The service role's trust policy, rewritten in place.
        #
        # Left alone, the construct emits TWO `sts:AssumeRole` statements for
        # bedrock-agentcore.amazonaws.com: one correctly scoped, and one with no
        # condition at all, which grants everything the scoped one refuses.
        # Confirmed on the deployed role. Anyone reading the scoped half would
        # conclude the role was hardened when it was not.
        #
        # This was first fixed by building the role explicitly and passing
        # `role=` to the Gateway. That FAILED TO DEPLOY, and the reason is worth
        # keeping: moving the Gateway to a new role also moves
        # `GatewayPolicyEngineAccess` below off the old one, and the Gateway's
        # update re-runs its policy-engine association while still assuming the
        # OLD role, which by then has lost `bedrock-agentcore:AuthorizeAction`.
        # The association 403s, the Gateway never stabilizes, and the whole stack
        # rolls back:
        #
        #   User: .../ToolsGatewayServiceRole1C-.../GenesisPolicyEngineAssociation
        #   is not authorized to perform: bedrock-agentcore:AuthorizeAction
        #
        # Overriding the document on the role the Gateway already has avoids the
        # swap entirely: same role ARN, same attached policies, no window where
        # the association lacks a permission. An escape hatch rather than an L2
        # property because the construct exposes no way to condition the trust it
        # generates.
        # ------------------------------------------------------------------
        cfn_gateway_role = gateway.role.node.default_child
        cfn_gateway_role.add_property_override(
            "AssumeRolePolicyDocument",
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AllowBedrockAgentCoreAssumeRole",
                        "Effect": "Allow",
                        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": self.account},
                            # Scoped to this Gateway's own name prefix, which is
                            # what the construct's own (defeated) statement used.
                            # The id is the name plus a random suffix, so a prefix
                            # is as tight as this can get without a cycle.
                            "ArnLike": {
                                "aws:SourceArn": (
                                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                                    f":gateway/{GATEWAY_NAME}*"
                                )
                            },
                        },
                    }
                ],
            },
        )

        tool_schema = agentcore.ToolSchema.from_local_asset(
            _stage_tool_definitions(tool_schema_path)
        )
        gateway_target = agentcore.GatewayTarget.for_lambda(
            self,
            "ToolsTarget",
            gateway=gateway,
            gateway_target_name=GATEWAY_TARGET_NAME,
            description="record_item / escalate / complete / transfer_to_team, backed by the tools Lambda",
            lambda_function=tools_lambda,
            tool_schema=tool_schema,
        )
        # Attached after the target exists: the Gateway's default service role's
        # DefaultPolicy only gains the tool-schema S3 read and Lambda-invoke
        # statements once GatewayTarget.for_lambda() grants them -- same
        # ordering agentcore_stack.py's own suppression relies on.
        gateway_grant_suppression = {
            "id": "AwsSolutions-IAM5",
            "reason": (
                "Granted to the Gateway's service role by "
                "GatewayTarget.for_lambda(), not chosen here: "
                "s3:GetBucket*/GetObject*/List* and the CDK asset bucket's "
                "object-key wildcard to read the uploaded tool schema, and the "
                "Lambda target's '<Arn>:*' suffix to invoke any published "
                "version/alias of the tools Lambda."
            ),
            "appliesTo": [
                "Action::s3:GetBucket*",
                "Action::s3:GetObject*",
                "Action::s3:List*",
                {"regex": "/^Resource::.*cdk-hnb659fds-assets.*\\*$/"},
                {"regex": "/^Resource::<ToolsLambda.*\\.Arn>:\\*$/"},
            ],
        }
        NagSuppressions.add_resource_suppressions(
            gateway, [gateway_grant_suppression], apply_to_children=True
        )

        # ------------------------------------------------------------------
        # Policy engine: one policy per statement file under policies_dir
        # (policies/dogwood/*.dw by default). Unchanged content -- see this
        # module's docstring.
        # ------------------------------------------------------------------
        policy_engine = agentcore.CfnPolicyEngine(
            self,
            "ToolsPolicyEngine",
            name=f"{RESOURCE_PREFIX}_policy_engine".replace("-", "_"),
            description=(
                "Dogwood policies gating Outbound IVR's tool calls through the Gateway"
                if policy_language == "dogwood"
                else "Cedar policies gating Outbound IVR's tool calls through the Gateway"
            ),
        )
        if policy_language == "dogwood":
            statement_dir = os.path.join(policies_dir, "dogwood")
            statement_files = sorted(glob.glob(os.path.join(statement_dir, "*.dw")))
        else:
            statement_files = sorted(glob.glob(os.path.join(policies_dir, "*.cedar")))
        self.policies = []
        permits, forbids = [], []
        for path in statement_files:
            stem = os.path.splitext(os.path.basename(path))[0]
            policy_name = f"{RESOURCE_PREFIX}_{stem}".replace("-", "_")
            with open(path, encoding="utf-8") as handle:
                statement = handle.read().replace("${GATEWAY_ARN}", gateway.gateway_arn)
            if policy_language == "dogwood":
                definition = agentcore.CfnPolicy.PolicyDefinitionProperty(
                    policy=agentcore.CfnPolicy.PolicyStatementProperty(statement=statement)
                )
            else:
                definition = agentcore.CfnPolicy.PolicyDefinitionProperty(
                    cedar=agentcore.CfnPolicy.CedarPolicyProperty(statement=statement)
                )
            policy = agentcore.CfnPolicy(
                self,
                f"Policy{stem.title().replace('-', '').replace('_', '')}",
                name=policy_name,
                policy_engine_id=policy_engine.attr_policy_engine_id,
                definition=definition,
            )
            policy.node.add_dependency(policy_engine)
            policy.node.add_dependency(gateway)
            self.policies.append(policy)
            if re.search(r"^\s*forbid\s*\(", statement, re.MULTILINE):
                forbids.append(policy)
            else:
                permits.append(policy)
        # The engine rejects a policy that, combined with what it already holds,
        # denies every request for its action ("Overly Restrictive"). A forbid
        # created before its action's permit does exactly that, so forbids wait
        # for every permit.
        for forbid in forbids:
            for permit in permits:
                forbid.node.add_dependency(permit)

        # No L2 for attaching a policy engine to a Gateway yet; drop to the L1
        # escape hatch on the Gateway's underlying CfnGateway -- same as
        # agentcore_stack.py.
        cfn_gateway = gateway.node.default_child
        cfn_gateway.add_property_override(
            "PolicyEngineConfiguration",
            {"Arn": policy_engine.attr_policy_engine_arn, "Mode": "ENFORCE"},
        )
        cfn_gateway.node.add_dependency(policy_engine)

        # The Gateway's service role evaluates the policies itself, and
        # CreateGateway calls GetPolicyEngine with it, so these must exist
        # before the Gateway does (policy-permissions.html). Its own ARN is
        # matched by name prefix, not gateway.gateway_arn, which would make
        # the role's policy depend on the Gateway that depends on it.
        # GetWorkloadAccessToken is for the temporal (once-per-session) rule.
        gateway_arn_prefix = (
            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/{GATEWAY_NAME}-*"
        )
        policy_access = iam.Policy(
            self,
            "GatewayPolicyEngineAccess",
            roles=[gateway.role],
            statements=[
                iam.PolicyStatement(
                    sid="PolicyEngineConfiguration",
                    actions=["bedrock-agentcore:GetPolicyEngine"],
                    resources=[policy_engine.attr_policy_engine_arn],
                ),
                iam.PolicyStatement(
                    sid="PolicyEngineAuthorization",
                    actions=[
                        "bedrock-agentcore:AuthorizeAction",
                        "bedrock-agentcore:PartiallyAuthorizeActions",
                    ],
                    resources=[policy_engine.attr_policy_engine_arn, gateway_arn_prefix],
                ),
                iam.PolicyStatement(
                    sid="PolicySessionWorkloadIdentity",
                    actions=["bedrock-agentcore:GetWorkloadAccessToken"],
                    resources=[
                        (
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                            ":workload-identity-directory/default"
                        ),
                        (
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                            f":workload-identity-directory/default/workload-identity/{GATEWAY_NAME}-*"
                        ),
                    ],
                ),
            ],
        )
        cfn_gateway.node.add_dependency(policy_access)
        NagSuppressions.add_resource_suppressions(
            policy_access,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The Gateway's id is its name plus a generated suffix, "
                        "known only after it exists; naming the Gateway's own "
                        "ARN here would be a dependency cycle."
                    ),
                    "appliesTo": [
                        {"regex": "/^Resource::arn:aws:bedrock-agentcore:.*-\\*$/"},
                    ],
                }
            ],
        )

        self.tools_lambda = tools_lambda
        self.gateway = gateway
        self.gateway_url = gateway.gateway_url or ""
        self.gateway_target = gateway_target
        self.policy_engine = policy_engine

        Tags.of(self).add("project", PROJECT_TAG)


def _build_tools_lambda_asset(
    lambda_dir: str, outbound_ivr_package_dir: str, extra_files: tuple[str, ...] = ()
) -> str:
    """Same staging shape as `lambda_bundling.py`'s
    `build_outbound_ivr_tools_lambda_asset` (stage handler.py + extra_files + the real
    `outbound_ivr.tools` package into a throwaway directory); shared with
    dashboard_construct.py's tools Lambda so both stay bundled identically."""
    from .lambda_bundling import build_outbound_ivr_tools_lambda_asset

    return build_outbound_ivr_tools_lambda_asset(
        lambda_dir, outbound_ivr_package_dir, extra_files=extra_files
    )


def _stage_tool_definitions(schema_path: str) -> str:
    """The Gateway reads a Lambda target's S3 tool schema as a bare JSON array
    of tool definitions; schema.json wraps that array in {"_comment", "tools"}
    (the tools Lambda bundles it as-is). Stage just the array for the target."""
    with open(schema_path, encoding="utf-8") as handle:
        tools = json.load(handle)["tools"]
    staged = os.path.join(tempfile.mkdtemp(prefix="outbound-ivr-tool-schema-"), "tools.json")
    with open(staged, "w", encoding="utf-8") as handle:
        json.dump(tools, handle, indent=2)
    return staged
