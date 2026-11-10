"""outbound-ivr-network: the VPC shared by the LiveKit compute, ElastiCache, and the
Outbound IVR Runtime.

A `NestedStack` (one of `outbound_ivr_stack.py`'s children), not a top-level
`cdk.Stack`-wrapped plain holder -- see outbound_ivr_stack.py's module docstring.
Deliberately minimal: no NAT, no SQS endpoint (the dashboard Lambda calls
`InvokeAgentRuntime` directly, no queue to drain), and no generic
`bedrock-agentcore` data-plane endpoint (the SDK's own async-task/HealthyBusy ping
and the `InvokeAgentRuntime` call both happen on connections the platform manages,
not a boto3 call this Runtime's own code makes).

Two interface endpoints *are* needed, since the Runtime calls AgentCore resources
directly from inside its own code, both confirmed against
`describe-vpc-endpoint-services`:
- `com.amazonaws.<region>.bedrock-agentcore.gateway` -- the Gateway's own MCP
  data-plane endpoint (`gateway_stack.py`'s Gateway), a distinct PrivateLink
  service from the generic `bedrock-agentcore` data-plane endpoint above, which
  does not cover Gateway traffic.
- `com.amazonaws.<region>.xray` -- ADOT's trace exporter (wired in by
  Workstream 4's `opentelemetry-instrument`/`StrandsTelemetry` changes) POSTs
  to `https://xray.<region>.amazonaws.com/v1/traces`, a different hostname
  from the existing CloudWatch Logs endpoint.
`ec2.InterfaceVpcEndpointAwsService` has named members for both
(`BEDROCK_AGENTCORE_GATEWAY`, `XRAY`) at the pinned `aws-cdk-lib` version
(2.273.0) -- no raw-service-name fallback needed.

Every interface endpoint below (Secrets Manager, both ECR endpoints, Bedrock
Runtime, CloudWatch Logs, the Gateway and X-Ray endpoints) lives in exactly one
private subnet, not every AZ's: each interface endpoint is a per-subnet ENI
billed per hour, and every subnet in a VPC already has the same local route to
any interface endpoint's ENI regardless of which subnet it was placed in (same
reasoning this file's own `SecretsManagerEndpoint` docstring already gave for
why the public subnet can reach it too) -- so a second AZ's ENI buys this app
nothing but a second hourly charge. Gateway endpoints (S3, DynamoDB) are free and
span every private subnet's route table regardless.
"""

from __future__ import annotations

from aws_cdk import NestedStack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from cdk_nag import NagSuppressions
from constructs import Construct

from .constants import DEFAULT_SONIC_MODEL, PROJECT_TAG, REGION, RESOURCE_PREFIX


def _sonic_model_arn(scope: Construct) -> str:
    from aws_cdk import Stack

    sonic_model = scope.node.try_get_context("sonicModel") or DEFAULT_SONIC_MODEL
    region = Stack.of(scope).region
    return f"arn:aws:bedrock:{region}::foundation-model/{sonic_model}"


class NetworkNestedStack(NestedStack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Explicit AZs, not `max_azs`: `max_azs` makes the CDK CLI look up this
        # account's actual AZs via a context provider (an AWS API call), which
        # fails synth with no AWS credentials at all (this app's own tests, and a
        # reader who only wants to `cdk synth`, both need to work with none). Every
        # commercial AWS Region has at least a and b AZs. Built from constants.REGION
        # (a plain string), not `self.region` -- a `NestedStack`'s own `region`
        # property is always the `AWS::Region` pseudo parameter token, deployed or
        # not, which `ec2.Vpc` can't use to validate subnet placement without
        # falling back to the same AWS-credentials-requiring lookup this avoids.
        availability_zones = [f"{REGION}a", f"{REGION}b"]
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"{RESOURCE_PREFIX}-vpc",
            availability_zones=availability_zones,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )
        NagSuppressions.add_resource_suppressions(
            self.vpc,
            [
                {
                    "id": "AwsSolutions-VPC7",
                    "reason": (
                        "A VPC for a handful of test calls; VPC Flow Logs add cost "
                        "and noise here with no log consumer."
                    ),
                }
            ],
        )

        self.public_subnets = self.vpc.public_subnets
        self.private_subnets = self.vpc.isolated_subnets
        self.private_subnet_selection = ec2.SubnetSelection(subnets=self.private_subnets)
        # Cost: every interface endpoint below lives in this one subnet
        # only (see module docstring), not every AZ's.
        single_private_subnet_selection = ec2.SubnetSelection(subnets=[self.private_subnets[0]])

        # Always on: the LiveKit host's own user-data fetches three secrets
        # at boot, and otherwise has no path to Secrets Manager except the
        # pool's Elastic IP, which can attach after user-data has already
        # started running -- see this module's docstring and
        # stacks/livekit_stack.py's retry loop around that same race.
        secrets_manager_endpoint = self.vpc.add_interface_endpoint(
            "SecretsManagerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            subnets=single_private_subnet_selection,
        )
        NagSuppressions.add_resource_suppressions(
            secrets_manager_endpoint,
            [
                {
                    "id": "CdkNagValidationFailure",
                    "reason": (
                        "AwsSolutions-EC23 cannot evaluate this endpoint's "
                        "security group: the ingress CIDR is the VPC's own "
                        "CIDR block via Fn::GetAtt, an intrinsic the rule "
                        "can't statically check -- not an open range."
                    ),
                }
            ],
            apply_to_children=True,
        )

        # The Outbound IVR Runtime runs in these isolated subnets and pulls (and
        # periodically refreshes) its container image from ECR; ECR serves
        # image layers from S3. AgentCore's VPC docs list these endpoints as
        # required when the subnets have no internet route.
        for endpoint_id, service in (
            ("EcrApiEndpoint", ec2.InterfaceVpcEndpointAwsService.ECR),
            ("EcrDockerEndpoint", ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER),
        ):
            endpoint = self.vpc.add_interface_endpoint(
                endpoint_id, service=service, subnets=single_private_subnet_selection
            )
            NagSuppressions.add_resource_suppressions(
                endpoint,
                [
                    {
                        "id": "CdkNagValidationFailure",
                        "reason": (
                            "AwsSolutions-EC23 cannot evaluate this endpoint's "
                            "security group: the ingress CIDR is the VPC's own "
                            "CIDR block via Fn::GetAtt, not an open range."
                        ),
                    }
                ],
                apply_to_children=True,
            )

        # Nova Sonic bidirectional streaming (bedrock:InvokeModelWithBidirectionalStream),
        # scoped to exactly the model this app's Runtime invokes.
        bedrock_runtime_endpoint = self.vpc.add_interface_endpoint(
            "BedrockRuntimeEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            subnets=single_private_subnet_selection,
        )
        bedrock_runtime_endpoint.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:InvokeModelWithBidirectionalStream",
                ],
                resources=[_sonic_model_arn(self)],
            )
        )
        NagSuppressions.add_resource_suppressions(
            bedrock_runtime_endpoint,
            [
                {
                    "id": "CdkNagValidationFailure",
                    "reason": (
                        "AwsSolutions-EC23 cannot evaluate this endpoint's "
                        "security group: the ingress CIDR is the VPC's own "
                        "CIDR block via Fn::GetAtt, not an open range."
                    ),
                }
            ],
            apply_to_children=True,
        )

        # On by default (unlike the old addLogsEndpoint-gated flag this app
        # used to carry): the real Outbound IVR agent, not a throwaway probe, needs
        # its own container logs reachable to debug a live demo.
        logs_endpoint = self.vpc.add_interface_endpoint(
            "LogsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            subnets=single_private_subnet_selection,
        )
        NagSuppressions.add_resource_suppressions(
            logs_endpoint,
            [
                {
                    "id": "CdkNagValidationFailure",
                    "reason": (
                        "AwsSolutions-EC23 cannot evaluate this endpoint's "
                        "security group: the ingress CIDR is the VPC's own "
                        "CIDR block via Fn::GetAtt, an intrinsic the rule "
                        "can't statically check -- not an open range."
                    ),
                }
            ],
            apply_to_children=True,
        )

        # The Gateway's own MCP data plane (gateway_stack.py's Gateway, called
        # directly by this Runtime's own agent code, not by the platform) and
        # ADOT's X-Ray trace-export endpoint (Workstream 4's observability
        # wiring) -- see this module's docstring for why both are needed and
        # confirmed present at the pinned CDK version.
        for endpoint_id, service in (
            (
                "BedrockAgentCoreGatewayEndpoint",
                ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE_GATEWAY,
            ),
            ("XRayEndpoint", ec2.InterfaceVpcEndpointAwsService.XRAY),
        ):
            agentcore_endpoint = self.vpc.add_interface_endpoint(
                endpoint_id, service=service, subnets=single_private_subnet_selection
            )
            NagSuppressions.add_resource_suppressions(
                agentcore_endpoint,
                [
                    {
                        "id": "CdkNagValidationFailure",
                        "reason": (
                            "AwsSolutions-EC23 cannot evaluate this endpoint's "
                            "security group: the ingress CIDR is the VPC's own "
                            "CIDR block via Fn::GetAtt, not an open range."
                        ),
                    }
                ],
                apply_to_children=True,
            )

        # Gateway endpoints: free, no ENI, span every subnet's route table
        # regardless -- S3 (case records/recordings) and DynamoDB (cases/
        # offices/events), per the shared contract.
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[self.private_subnet_selection],
        )
        self.vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            subnets=[self.private_subnet_selection],
        )

        Tags.of(self.vpc).add("project", PROJECT_TAG)
