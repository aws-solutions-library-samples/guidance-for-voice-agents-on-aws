"""OutboundIvrVoiceStack: the Chime SDK Voice Connector and its pool of two Elastic IPs
the self-hosted LiveKit host (livekit_stack.py) registers its outbound trunk through.
Slot 0 is unused (kept allocated for a future second host); slot 1 is the LiveKit host
actually deployed.

Deploy this stack first, on its own, before anything that reads its SSM parameters --
see the top-level README's "Deploy order". Everything here is tagged
`project=outbound-ivr` (see `VOICE_PROJECT_TAG` below, deliberately NOT
`constants.PROJECT_TAG` -- see that constant's own comment) and, like every other
stack in this example, uses `RemovalPolicy.DESTROY` so `cdk destroy` removes it
completely: both EIPs are released, and the Voice Connector custom resource below
explicitly calls `DeleteVoiceConnector` on delete (deleting the connector also removes
its termination settings and credentials, so those two don't need their own delete
calls). `infrastructure/scripts/cleanup.sh` runs this stack's destroy last, since
livekit_stack.py's outbound trunk registration depends on this one's SSM parameters.

Resources:
- One Chime SDK Voice Connector (`outbound-ivr-vc`): outbound (termination) only,
  `RequireEncryption: true` (TLS signaling, SRTP media), `CallingRegions: ["US"]`,
  `CpsLimit: 1`. No CloudFormation resource exists for a Voice Connector, so this
  is an `AwsCustomResource` calling the `ChimeSDKVoice` API directly. Termination
  credentials use a dedicated handler Lambda rather than another
  `AwsCustomResource`, so the credentials never pass through as a resolved value
  in the custom resource's own properties: it receives only the secret's ARN and
  fetches the value itself at invocation time. `on_update` calls
  `UpdateVoiceConnector` (not `CreateVoiceConnector` again): `CreateVoiceConnector`
  is a `POST` that allocates a brand-new `VoiceConnectorId` every time it is
  called, so re-running it on update would silently mint a second, duplicate
  connector instead of updating the one this stack already created. `on_delete`
  calls `DeleteVoiceConnector`, so `cdk destroy` actually removes it.
- A pool of two Elastic IPs (`Eip0`, `Eip1`). The Voice Connector's termination
  `CidrAllowedList` is exactly those two `/32`s. Slot 1 is for the LiveKit host
  actually deployed; slot 0 is unused, kept allocated for a future second host.
- Seven SSM String parameters under `/outbound-ivr/voice/*`, so livekit_stack.py's
  own, separate CDK app (which cannot use a cross-stack `Fn::ImportValue` on
  anything this stack creates) can read this stack's outputs by name. Every reader
  uses `ssm.StringParameter.value_for_string_parameter`/
  `from_string_parameter_name(...).string_value`, a CloudFormation dynamic
  reference resolved at *deploy* time, not a synth-time context lookup
  (`value_from_lookup`) -- so reading them never requires the parameters to
  already exist, which matters for a synth-only pass like this one's own tests.

IAM scoping for the Chime actions above: `chime:CreateVoiceConnector` is the one
action in this stack that uses a bare `resources=["*"]` (there is no Voice
Connector id yet to scope to when creating one). Every other Chime action
(`UpdateVoiceConnector`, `DeleteVoiceConnector`, `PutVoiceConnectorTermination`,
`PutVoiceConnectorTerminationCredentials`) is scoped to
`arn:aws:chime:<region>:<account>:vc/*` -- the broadest resource-level ARN
pattern the `chime` IAM namespace actually supports for a Voice Connector
resource (confirmed against the Chime SDK's IAM action reference: these actions
do support the `voiceconnector` resource type; only `CreateVoiceConnector`
cannot be scoped, for the reason above), not a full account-wide wildcard.
"""

from __future__ import annotations

import json

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as _lambda,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from aws_cdk import (
    custom_resources as cr,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from .constants import ACCOUNT_ID, REGION

# This stack's project tag is frozen at whatever value it was first deployed with, not
# the app-wide PROJECT_TAG from constants.py. The tag is not just metadata here: it
# sits inside the `createVoiceConnector` parameters of the custom resource below.
# Changing it changes that resource's Create call, which CloudFormation treats as a
# possible REPLACEMENT -- a brand new Voice Connector, the SSM parameters repointed at
# it, and the LiveKit host's trunk registration still pointed at the old one. Leave it
# fixed once deployed; `cdk diff` catches an accidental change before `cdk deploy` does.
VOICE_PROJECT_TAG = "outbound-ivr"

VOICE_CONNECTOR_NAME = "outbound-ivr-vc"

EIP_POOL_SIZE = 2
LIVEKIT_HOST_SLOT = 1

# Every Chime Voice Connector action except CreateVoiceConnector (which can't
# name a connector that doesn't exist yet, so there is no ARN to scope to
# before it is created) is scoped to this account's connectors, never a bare
# "*" -- see this module's docstring, "IAM scoping".
CHIME_VOICE_CONNECTOR_ARN_PATTERN = f"arn:aws:chime:{REGION}:{ACCOUNT_ID}:vc/*"

# SSM parameter paths this stack publishes. The consumer (livekit_stack.py's LiveKit
# host, slot 1) reads these by name with deploy-time resolution -- never a synth-time
# lookup, so nothing on either side of this needs AWS credentials to synth, and
# OutboundIvrVoiceStack must simply deploy before OutboundIvrStack does (see the
# top-level README's "Deploy order").
SSM_PREFIX = "/outbound-ivr/voice"
PARAM_CONNECTOR_ID = f"{SSM_PREFIX}/connector-id"
PARAM_OUTBOUND_HOST = f"{SSM_PREFIX}/outbound-host"
PARAM_CREDENTIALS_SECRET_ARN = f"{SSM_PREFIX}/credentials-secret-arn"
PARAM_CALLER_ID = f"{SSM_PREFIX}/caller-id"


def eip_allocation_id_param(slot: int) -> str:
    return f"{SSM_PREFIX}/eip/{slot}/allocation-id"


def eip_public_ip_param(slot: int) -> str:
    return f"{SSM_PREFIX}/eip/{slot}/public-ip"


class VoiceStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # Elastic IP pool: two addresses, DESTROY (the CfnEIP default) --
        # released when this stack is destroyed.
        # ------------------------------------------------------------------
        eips: list[ec2.CfnEIP] = []
        for slot in range(EIP_POOL_SIZE):
            eip = ec2.CfnEIP(self, f"Eip{slot}", domain="vpc")
            eip.apply_removal_policy(RemovalPolicy.DESTROY)
            Tags.of(eip).add("project", VOICE_PROJECT_TAG)
            eips.append(eip)
        self.eips = eips

        # ------------------------------------------------------------------
        # Secret: SIP digest termination credentials for the Voice
        # Connector's outbound trunk. Generated, never a literal.
        # ------------------------------------------------------------------
        credentials_secret = secretsmanager.Secret(  # nosec B106 -- secret_name is the secret's name, not its value
            self,
            "VoiceConnectorCredentialsSecret",
            secret_name="outbound-ivr/voice-connector-credentials",
            description=(
                "Generated SIP digest termination credentials for the "
                "outbound-ivr-vc Voice Connector's outbound trunk"
            ),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "outbound-ivr-trunk"}),
                generate_string_key="password",
                exclude_punctuation=True,
                password_length=32,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        NagSuppressions.add_resource_suppressions(
            credentials_secret,
            [
                {
                    "id": "AwsSolutions-SMG4",
                    "reason": (
                        "No automatic rotation: this credential is pinned into "
                        "Chime's termination settings by the handler Lambda "
                        "below; rotating it means generating a new secret value "
                        "and re-running that Lambda, not an automated flow in "
                        "this example."
                    ),
                }
            ],
        )
        self.credentials_secret = credentials_secret

        # ------------------------------------------------------------------
        # Chime SDK Voice Connector: outbound (termination) only. See this
        # module's docstring for the delete mechanics and the IAM scoping.
        # ------------------------------------------------------------------
        create_params = {
            "Name": VOICE_CONNECTOR_NAME,
            "AwsRegion": REGION,
            "RequireEncryption": True,
            "Tags": [{"Key": "project", "Value": VOICE_PROJECT_TAG}],
        }
        update_params = {
            "VoiceConnectorId": cr.PhysicalResourceIdReference(),
            "Name": VOICE_CONNECTOR_NAME,
            "RequireEncryption": True,
        }
        delete_params = {"VoiceConnectorId": cr.PhysicalResourceIdReference()}
        voice_connector = cr.AwsCustomResource(
            self,
            "ChimeVoiceConnector",
            resource_type="Custom::ChimeVoiceConnector",
            install_latest_aws_sdk=True,
            removal_policy=RemovalPolicy.DESTROY,
            on_create=cr.AwsSdkCall(
                service="ChimeSDKVoice",
                action="createVoiceConnector",
                parameters=create_params,
                physical_resource_id=cr.PhysicalResourceId.from_response(
                    "VoiceConnector.VoiceConnectorId"
                ),
            ),
            # UpdateVoiceConnector, not CreateVoiceConnector again --
            # CreateVoiceConnector is not idempotent (it mints a brand-new
            # VoiceConnectorId every call); see the module docstring.
            on_update=cr.AwsSdkCall(
                service="ChimeSDKVoice",
                action="updateVoiceConnector",
                parameters=update_params,
                physical_resource_id=cr.PhysicalResourceId.from_response(
                    "VoiceConnector.VoiceConnectorId"
                ),
            ),
            # DeleteVoiceConnector, so `cdk destroy` actually removes it. Deleting the
            # connector also removes its termination settings and credentials, so
            # neither of those two resources below needs its own delete call.
            on_delete=cr.AwsSdkCall(
                service="ChimeSDKVoice",
                action="deleteVoiceConnector",
                parameters=delete_params,
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        sid="CreateVoiceConnector",
                        actions=["chime:CreateVoiceConnector"],
                        # The one Chime action this account's security rules
                        # allow "*" for: it names a connector that does not
                        # exist yet, so there is no ARN to scope to before
                        # this call creates it.
                        resources=["*"],
                    ),
                    # CreateVoiceConnector with Tags is also authorized as
                    # chime:TagResource on the new connector.
                    iam.PolicyStatement(
                        sid="TagVoiceConnector",
                        actions=["chime:TagResource"],
                        resources=[CHIME_VOICE_CONNECTOR_ARN_PATTERN],
                    ),
                    iam.PolicyStatement(
                        sid="UpdateVoiceConnector",
                        actions=["chime:UpdateVoiceConnector"],
                        resources=[CHIME_VOICE_CONNECTOR_ARN_PATTERN],
                    ),
                    iam.PolicyStatement(
                        sid="DeleteVoiceConnector",
                        actions=["chime:DeleteVoiceConnector"],
                        resources=[CHIME_VOICE_CONNECTOR_ARN_PATTERN],
                    ),
                ]
            ),
        )
        voice_connector_id = voice_connector.get_response_field("VoiceConnector.VoiceConnectorId")
        self.voice_connector_id = voice_connector_id
        self.outbound_host = voice_connector.get_response_field("VoiceConnector.OutboundHostName")

        # Outbound caller ID. Chime rejects any caller ID that is not a Chime
        # SDK number in this account ("403 Caller ID is not a valid number"),
        # and only honors it as the Voice Connector's default number. The
        # number itself is ordered once outside CDK (there is no CloudFormation
        # resource for a phone number order) and passed in as context.
        caller_id = self.node.try_get_context("callerIdNumber")
        termination = {
            "CallingRegions": ["US"],
            "CidrAllowedList": [f"{eip.attr_public_ip}/32" for eip in eips],
            "CpsLimit": 1,
            "Disabled": False,
        }
        if caller_id:
            termination["DefaultPhoneNumber"] = caller_id
        termination_params = {"VoiceConnectorId": voice_connector_id, "Termination": termination}
        termination = cr.AwsCustomResource(
            self,
            "ChimeVoiceConnectorTermination",
            resource_type="Custom::ChimeVoiceConnectorTermination",
            install_latest_aws_sdk=True,
            removal_policy=RemovalPolicy.DESTROY,
            # No on_delete: deleting the Voice Connector itself (this resource's
            # DependsOn, below) removes its termination settings too, and
            # CloudFormation deletes dependents like this one before the resource
            # it depends on.
            on_create=cr.AwsSdkCall(
                service="ChimeSDKVoice",
                action="putVoiceConnectorTermination",
                parameters=termination_params,
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{VOICE_CONNECTOR_NAME}-termination"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="ChimeSDKVoice",
                action="putVoiceConnectorTermination",
                parameters=termination_params,
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{VOICE_CONNECTOR_NAME}-termination"
                ),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["chime:PutVoiceConnectorTermination"],
                        resources=[CHIME_VOICE_CONNECTOR_ARN_PATTERN],
                    )
                ]
            ),
        )
        termination.node.add_dependency(voice_connector)

        # ------------------------------------------------------------------
        # Termination credentials: a dedicated handler Lambda, not another
        # AwsCustomResource, so the credentials never pass through as a resolved
        # value in the custom resource's own properties. This Lambda receives only
        # the secret's ARN (not sensitive on its own) and fetches the value itself
        # at invocation time; it never logs what it fetched. The Delete branch is a
        # no-op, not a call to deleteVoiceConnectorTerminationCredentials: deleting
        # the Voice Connector (this resource's dependency, below) already removes
        # its termination credentials.
        # ------------------------------------------------------------------
        termination_credentials_handler = _lambda.Function(
            self,
            "ChimeTerminationCredentialsHandler",
            runtime=_lambda.Runtime.PYTHON_3_14,
            handler="index.handler",
            timeout=Duration.seconds(30),
            code=_lambda.Code.from_inline(
                """
import json

import boto3

chime = boto3.client("chime-sdk-voice")
secretsmanager = boto3.client("secretsmanager")


def handler(event, _context):
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    voice_connector_id = props["VoiceConnectorId"]
    physical_id = f"{voice_connector_id}-termination-credentials"

    if request_type == "Delete":
        # Nothing to do: deleting the Voice Connector itself already removes
        # its termination credentials -- see voice_stack.py's module docstring.
        return {"PhysicalResourceId": physical_id}

    secret = secretsmanager.get_secret_value(SecretId=props["SecretArn"])
    credentials = json.loads(secret["SecretString"])
    chime.put_voice_connector_termination_credentials(
        VoiceConnectorId=voice_connector_id,
        Credentials=[
            {
                "Username": credentials["username"],
                "Password": credentials["password"],
            }
        ],
    )
    return {"PhysicalResourceId": physical_id}
"""
            ),
        )
        credentials_secret.grant_read(termination_credentials_handler)
        termination_credentials_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["chime:PutVoiceConnectorTerminationCredentials"],
                resources=[CHIME_VOICE_CONNECTOR_ARN_PATTERN],
            )
        )
        NagSuppressions.add_resource_suppressions(
            termination_credentials_handler,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWSLambdaBasicExecutionRole (CloudWatch Logs only), "
                        "the standard CDK default for a Lambda with no custom "
                        "role passed in; no customer-authored equivalent."
                    ),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "secretsmanager:GetSecretValue via grant_read() is "
                        "scoped to exactly the one Voice Connector credentials "
                        "secret (plus its AWS-managed KMS decrypt grant); no "
                        "wildcard resource. "
                        "chime:PutVoiceConnectorTerminationCredentials is "
                        "scoped to arn:aws:chime:<region>:<account>:vc/*, the "
                        "broadest resource-level ARN this action actually "
                        "supports -- see the module docstring's 'IAM scoping' "
                        "paragraph."
                    ),
                    "appliesTo": [
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:Decrypt*",
                        f"Resource::{CHIME_VOICE_CONNECTOR_ARN_PATTERN}",
                    ],
                },
            ],
            apply_to_children=True,
        )

        termination_credentials_provider = cr.Provider(
            self,
            "ChimeTerminationCredentialsProvider",
            on_event_handler=termination_credentials_handler,
        )
        NagSuppressions.add_resource_suppressions(
            termination_credentials_provider,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "The Provider framework's own 'framework-onEvent' "
                        "Lambda uses the AWS-authored AWSLambdaBasicExecutionRole; "
                        "CDK-internal, no customer-authored equivalent."
                    ),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The Provider framework grants its own "
                        "'framework-onEvent' Lambda permission to invoke exactly "
                        "the one handler Lambda above (a versioned/aliased ARN "
                        "pattern); CDK-internal, not something this stack "
                        "widened."
                    ),
                    "appliesTo": [
                        {"regex": "/^Resource::<ChimeTerminationCredentialsHandler.*\\.Arn>:\\*$/"}
                    ],
                },
            ],
            apply_to_children=True,
        )

        termination_credentials = CustomResource(
            self,
            "ChimeVoiceConnectorTerminationCredentials",
            resource_type="Custom::ChimeVoiceConnectorTerminationCredentials",
            service_token=termination_credentials_provider.service_token,
            removal_policy=RemovalPolicy.DESTROY,
            properties={
                # Never the secret's value: the handler above fetches it
                # itself from this ARN at invocation time.
                "VoiceConnectorId": voice_connector_id,
                "SecretArn": credentials_secret.secret_arn,
            },
        )
        termination_credentials.node.add_dependency(termination)

        for cr_construct in (voice_connector, termination):
            NagSuppressions.add_resource_suppressions(
                cr_construct,
                [
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": (
                            "AwsCustomResource's generated Lambda uses the "
                            "AWS-authored AWSLambdaBasicExecutionRole for "
                            "CloudWatch Logs only; this is the standard CDK "
                            "custom-resource pattern."
                        ),
                    },
                    {
                        "id": "AwsSolutions-IAM5",
                        "reason": (
                            "chime:CreateVoiceConnector has no resource-level "
                            "permissions to scope to (no connector id exists "
                            "yet); chime:UpdateVoiceConnector, "
                            "chime:DeleteVoiceConnector, and "
                            "chime:PutVoiceConnectorTermination are all "
                            "scoped to arn:aws:chime:<region>:<account>:vc/*, "
                            "the broadest resource-level ARN they support -- "
                            "see the module docstring's 'IAM scoping' "
                            "paragraph."
                        ),
                        "appliesTo": [
                            "Resource::*",
                            f"Resource::{CHIME_VOICE_CONNECTOR_ARN_PATTERN}",
                        ],
                    },
                ],
                apply_to_children=True,
            )

        # The two AwsCustomResources above share one CDK-internal singleton
        # Lambda (the generic "AWS SDK call" custom resource provider),
        # parented directly under this stack rather than under either of
        # them -- so the per-construct suppressions just above do not reach
        # its role. Same AWSLambdaBasicExecutionRole reasoning, applied at
        # the stack level to reach it regardless of its generated
        # (hash-based, not stable across CDK versions) logical ID.
        NagSuppressions.add_stack_suppressions(
            self,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "The only stack-level IAM4 finding left after the "
                        "suppressions above is AwsCustomResource's shared "
                        "singleton provider Lambda's AWSLambdaBasicExecutionRole "
                        "-- the AWS-documented shape for a custom resource "
                        "provider; no customer-authored equivalent."
                    ),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                }
            ],
        )

        # ------------------------------------------------------------------
        # SSM parameters: how the LiveKit deployment's own, separate CDK app reads
        # this stack's outputs -- see the module docstring's "Resources" and
        # third paragraph.
        # ------------------------------------------------------------------
        ssm.StringParameter(
            self,
            "ConnectorIdParam",
            parameter_name=PARAM_CONNECTOR_ID,
            string_value=voice_connector_id,
            description="The outbound-ivr-vc Chime SDK Voice Connector's id",
        )
        ssm.StringParameter(
            self,
            "OutboundHostParam",
            parameter_name=PARAM_OUTBOUND_HOST,
            string_value=self.outbound_host,
            description="outbound-ivr-vc's outbound (termination) SIP host; the target for any outbound trunk registration",
        )
        ssm.StringParameter(
            self,
            "CredentialsSecretArnParam",
            parameter_name=PARAM_CREDENTIALS_SECRET_ARN,
            string_value=credentials_secret.secret_arn,
            description=(
                "ARN of the Secrets Manager secret holding outbound-ivr-vc's SIP "
                "digest termination credentials -- never the value itself, "
                "fetch it with secretsmanager:GetSecretValue"
            ),
        )
        if caller_id:
            ssm.StringParameter(
                self,
                "CallerIdParam",
                parameter_name=PARAM_CALLER_ID,
                string_value=caller_id,
                description="outbound-ivr-vc's outbound caller ID, a Chime SDK number on this connector",
            )
        for slot, eip in enumerate(eips):
            ssm.StringParameter(
                self,
                f"Eip{slot}AllocationIdParam",
                parameter_name=eip_allocation_id_param(slot),
                string_value=eip.attr_allocation_id,
                description=f"Pool slot {slot}'s Elastic IP allocation id",
            )
            ssm.StringParameter(
                self,
                f"Eip{slot}PublicIpParam",
                parameter_name=eip_public_ip_param(slot),
                string_value=eip.attr_public_ip,
                description=f"Pool slot {slot}'s Elastic IP public address",
            )

        Tags.of(self).add("project", VOICE_PROJECT_TAG)

        CfnOutput(self, "VoiceConnectorId", value=voice_connector_id)
        CfnOutput(self, "VoiceConnectorOutboundHost", value=self.outbound_host)
        for slot, eip in enumerate(eips):
            CfnOutput(self, f"Eip{slot}PublicIp", value=eip.attr_public_ip)
