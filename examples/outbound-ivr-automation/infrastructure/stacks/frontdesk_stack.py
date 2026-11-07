"""FrontDeskNestedStack: not the front desk any more -- the ONE piece of the mock
office that cannot move out of OutboundIvrStack, the dashboard's CCP (Contact Control
Panel) access grant.

The mock office lives out of OutboundIvrStack entirely, in `OutboundIvrOfficeStack`, a
top-level `Stack` deployed on its own, before and independently of OutboundIvrStack.
Hours of operation, the front desk queue, the
security profile, the routing profile, the `outbound-ivr-front-desk` agent user and its
password secret, the office IVR contact flow, and the office DID (plus its flow
association) all move there -- long-lived, so tearing down OutboundIvrStack never
releases a claimed DID. That also kills the two-pass deploy outbound_ivr_stack.py's own
wiring comments used to describe (deploy once to claim the office number,
copy FrontDesk's CfnOutput by hand into `-c officeNumber=...`, deploy again):
`OutboundIvrOfficeStack` claims the number, if any, on its own, independent deploy,
so it exists -- claimed or the documented placeholder -- before OutboundIvrStack is
ever synthesized, and AgentRuntime no longer has to wait on a FrontDesk built
after it in the same stack to learn it.

`OutboundIvrOfficeStack` publishes four SSM parameters unconditionally, every deploy --
`/outbound-ivr/connect/instance-id`, `/outbound-ivr/connect/instance-arn`,
`/outbound-ivr/connect/office-number`, and `/outbound-ivr/connect/front-desk-queue-arn` -- and
`outbound_ivr_stack.py` reads them with
`ssm.StringParameter.from_string_parameter_name(...).string_value`, the same
dynamic-reference pattern `livekit_stack.py` already uses for the nine frozen
`/outbound-ivr/voice/*` parameters. That SSM read is NOT a CloudFormation dependency,
which is what makes the split possible: nothing in OutboundIvrStack has to wait on
OutboundIvrOfficeStack at deploy time.

This class is what is left after that move: the one resource that genuinely
cannot live in OutboundIvrOfficeStack, because it needs something OutboundIvrOfficeStack has
no way to know -- the dashboard's own CloudFront domain, which only exists
inside OutboundIvrStack, built after AgentRuntime and Dashboard. (The Connect instance
id is also an input here, but this file never touches SSM itself -- OutboundIvrStack
reads it off `/outbound-ivr/connect/instance-id` and hands it straight into the
constructor below.)

**CloudFormation has no approved-origin resource.** Checked against the
`AWS::Connect::*` resource catalog (Instance, ContactFlow, HoursOfOperation,
PhoneNumber, Queue, RoutingProfile, SecurityProfile, User, UserHierarchyGroup,
IntegrationAssociation, ... -- no `ApprovedOrigin`): the dashboard's CloudFront
origin is registered with `connect:AssociateApprovedOrigin` through an
`AwsCustomResource` instead, `disassociateApprovedOrigin` on delete.

**`instance_id`, not `instance_arn`.** `AssociateApprovedOrigin` and
`DisassociateApprovedOrigin` both take an `InstanceId` parameter documented as
the Connect instance's identifier, not its ARN -- unlike the native
`AWS::Connect::*` resources this file used to also create (which took
`InstanceArn` directly, and have all moved to `OutboundIvrOfficeStack` now). With
only the custom resource left, the constructor takes `instance_id: str` to
match the one API it still calls.

**Still synth only.** Same ground rules as the rest of this app: no `cdk deploy`.
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    NestedStack,
    Stack,
    Tags,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    custom_resources as cr,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from .constants import ACCOUNT_ID, PROJECT_TAG, RESOURCE_PREFIX


class FrontDeskNestedStack(NestedStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        instance_id: str,
        dashboard_origin: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # Approved origin: lets the dashboard's embedded CCP (amazon-connect-streams,
        # loaded from the CloudFront-hosted SPA) call this instance's CCP endpoints
        # from that origin -- Connect's CORS allowlist for the Streams API. No
        # native CloudFormation resource for it (module docstring), so an
        # AwsCustomResource calls the Connect API directly, its IAM scoped to this
        # one instance (both actions are authorized against the instance ARN).
        # ------------------------------------------------------------------
        approved_origin = cr.AwsCustomResource(
            self,
            "DashboardApprovedOrigin",
            resource_type="Custom::ConnectApprovedOrigin",
            install_latest_aws_sdk=True,
            on_create=cr.AwsSdkCall(
                service="Connect",
                action="associateApprovedOrigin",
                parameters={"InstanceId": instance_id, "Origin": dashboard_origin},
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{RESOURCE_PREFIX}-front-desk-approved-origin"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="Connect",
                action="associateApprovedOrigin",
                parameters={"InstanceId": instance_id, "Origin": dashboard_origin},
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{RESOURCE_PREFIX}-front-desk-approved-origin"
                ),
            ),
            on_delete=cr.AwsSdkCall(
                service="Connect",
                action="disassociateApprovedOrigin",
                parameters={"InstanceId": instance_id, "Origin": dashboard_origin},
                ignore_error_codes_matching="ResourceNotFoundException",
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=[
                            "connect:AssociateApprovedOrigin",
                            "connect:DisassociateApprovedOrigin",
                        ],
                        resources=[
                            Stack.of(self).format_arn(
                                service="connect",
                                resource="instance",
                                resource_name=instance_id,
                            )
                        ],
                    )
                ]
            ),
        )
        NagSuppressions.add_resource_suppressions(
            approved_origin,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AwsCustomResource's generated Lambda uses the "
                        "AWS-authored AWSLambdaBasicExecutionRole for "
                        "CloudWatch Logs only; standard CDK custom-resource "
                        "pattern, same as escalation_stack.py's equivalent "
                        "association."
                    ),
                },
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_stack_suppressions(
            self,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "Covers the AwsCustomResource framework's shared "
                        "singleton provider Lambda, which AWS CDK attaches "
                        "AWSLambdaBasicExecutionRole to for CloudWatch Logs "
                        "only; no customer-authored equivalent."
                    ),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                }
            ],
        )

        # Derived from constants, same expression outbound_ivr_stack.py builds its own
        # copy from (never read off OutboundIvrOfficeStack, which has no concept of
        # "CCP url" to begin with -- just the instance alias) -- kept so neither
        # side takes a dependency on the other for a string either one can
        # already compute. See outbound_ivr_stack.py's own module docstring.
        self.ccp_url = f"https://{RESOURCE_PREFIX}-office-{ACCOUNT_ID}.my.connect.aws/ccp-v2/"

        Tags.of(self).add("project", PROJECT_TAG)

        CfnOutput(self, "FrontDeskCcpUrl", value=self.ccp_url)
