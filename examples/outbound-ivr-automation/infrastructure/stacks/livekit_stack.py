"""outbound-ivr-livekit: livekit-server + livekit-sip on ECS Managed Instances (host
networking), and ElastiCache Serverless (Valkey) as LiveKit's Redis. See the
top-level README's cost section for what each piece costs.

A `NestedStack` (one of `outbound_ivr_stack.py`'s children), not a top-level
`cdk.Stack` -- see outbound_ivr_stack.py's module docstring.

**A private DNS name for the host, not a fixed IP.** The Outbound IVR Runtime reaches
livekit-server on its private address, which the Auto Scaling group picks at launch
(Auto Scaling rejects a launch template that pins a private IP). So this stack owns a
Route 53 private hosted zone in the VPC, and the same Lambda that attaches the
Elastic IP also UPSERTs `livekit.<zone>` to the host's private IP whenever the task
lands on an instance. The Runtime connects to that name; a replacement host updates
the record, with no deploy-order race. The Lambda also answers a CloudFormation
custom resource, deleting the record on stack deletion so the zone can be deleted.

**ECS Managed Instances, not a plain EC2 instance.** One ECS cluster, one
`ManagedInstancesCapacityProvider` in the public subnet, one service running one task
in host network mode, and an EIP-associator Lambda triggered off the task reaching
RUNNING (ECS Managed Instances gives AWS the EC2 instance's own lifecycle, so there is
no synth-time instance ID to attach the Elastic IP to declaratively with a
`CfnEIPAssociation`; the associator Lambda does it at runtime instead). ECS Exec
(`EnableExecuteCommand`) is how to shell into the running task for debugging -- there
is no bare EC2 instance to SSM into, and no SSH port or key pair either way.

**No Voice Connector or Elastic IP of its own.** This stack consumes pool slot 1 of
the permanent `OutboundIvrVoiceStack` stack (`voice_stack.py`): the EIP association
targets `/outbound-ivr/voice/eip/1/allocation-id` via the associator Lambda, and the
outbound trunk (registered by the trunk-setup container, not this CDK app) targets
`/outbound-ivr/voice/outbound-host` with the digest credentials at
`/outbound-ivr/voice/credentials-secret-arn`. Every one of those SSM values is read with
`ssm.StringParameter.from_string_parameter_name(...).string_value` /
`value_for_string_parameter`, a CloudFormation dynamic reference resolved by
CloudFormation at *deploy* time -- never a synth-time context lookup -- so `cdk synth`
never requires `OutboundIvrVoiceStack` to already be deployed. It still must be,
before this stack actually deploys -- see the top-level README's "Deploy order".
"""

from __future__ import annotations

import json

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    NestedStack,
    RemovalPolicy,
    SecretValue,
    Tags,
)
from aws_cdk import (
    aws_autoscaling as autoscaling,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecs as ecs,
)
from aws_cdk import (
    aws_elasticache as elasticache,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as targets,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_lambda as _lambda,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_route53 as route53,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from .constants import (
    ACCOUNT_ID,
    CHIME_VOICE_CONNECTOR_CIDR,
    LIVEKIT_SIGNALING_PORT,
    LIVEKIT_TCP_FALLBACK_PORT,
    PROJECT_TAG,
    REGION,
    RESOURCE_PREFIX,
    SIP_RTP_PORT_RANGE,
    SIP_TLS_PORT,
    VALKEY_PORT,
    WEBRTC_UDP_PORT_RANGE,
)

# The pool slot this app's host uses -- see voice_stack.py's module docstring.
LIVEKIT_EIP_SLOT = 1

# The private DNS name the Outbound IVR Runtime uses for livekit-server (module docstring).
PRIVATE_ZONE_NAME = "outbound_ivr.internal"
LIVEKIT_RECORD_NAME = f"livekit.{PRIVATE_ZONE_NAME}"


class LiveKitNestedStack(NestedStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        public_subnets: list[ec2.ISubnet],
        private_subnets: list[ec2.ISubnet],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.livekit_host = LIVEKIT_RECORD_NAME
        zone = route53.PrivateHostedZone(self, "PrivateZone", zone_name=PRIVATE_ZONE_NAME, vpc=vpc)

        livekit_server_image = (
            self.node.try_get_context("livekitServerImage") or "livekit/livekit-server:v1.13.10"
        )
        livekit_sip_image = self.node.try_get_context("livekitSipImage") or "livekit/sip:v1.19.0"
        # No local Redis/Valkey image: ElastiCache Serverless is the cache (b),
        # so there is nothing to run on the host for it.
        cache_data_storage_max_gb = int(self.node.try_get_context("cacheDataStorageMaxGb") or 1)
        cache_ecpu_max_per_second = int(self.node.try_get_context("cacheEcpuMaxPerSecond") or 5000)

        # ------------------------------------------------------------------
        # Security groups. Compute: SIP TLS + Chime's SRTP range from the Chime
        # CIDR only; LiveKit's own signaling/WebRTC ports from the VPC CIDR only
        # (point (a), (e) -- the AgentCore Runtime test is the one VPC-internal
        # client). Cache: the Valkey port from the compute security group only,
        # never a CIDR.
        # ------------------------------------------------------------------
        compute_sg = ec2.SecurityGroup(
            self,
            "ComputeSecurityGroup",
            vpc=vpc,
            security_group_name=f"{RESOURCE_PREFIX}-livekit-sg",
            description="outbound_ivr: livekit-server + livekit-sip host",
            allow_all_outbound=True,
        )
        compute_sg.add_ingress_rule(
            ec2.Peer.ipv4(CHIME_VOICE_CONNECTOR_CIDR),
            ec2.Port.tcp(SIP_TLS_PORT),
            "Chime SDK Voice Connector SIP/TLS signaling",
        )
        compute_sg.add_ingress_rule(
            ec2.Peer.ipv4(CHIME_VOICE_CONNECTOR_CIDR),
            ec2.Port.udp_range(*SIP_RTP_PORT_RANGE),
            "Chime SDK Voice Connector SRTP media",
        )
        compute_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(LIVEKIT_SIGNALING_PORT),
            "LiveKit HTTP/WebSocket signaling, VPC-internal clients only",
        )
        compute_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(LIVEKIT_TCP_FALLBACK_PORT),
            "LiveKit WebRTC TCP fallback, VPC-internal clients only",
        )
        compute_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.udp_range(*WEBRTC_UDP_PORT_RANGE),
            "LiveKit WebRTC/ICE media, VPC-internal clients only",
        )

        NagSuppressions.add_resource_suppressions(
            compute_sg,
            [
                {
                    "id": "CdkNagValidationFailure",
                    "reason": (
                        "AwsSolutions-EC23 cannot evaluate the VPC-CIDR ingress "
                        "rules above: the CIDR is `vpc.vpc_cidr_block`, resolved "
                        "via Fn::GetAtt, an intrinsic the rule can't statically "
                        "check -- it is the VPC's own CIDR, not an open range."
                    ),
                }
            ],
        )
        cache_sg = ec2.SecurityGroup(
            self,
            "CacheSecurityGroup",
            vpc=vpc,
            security_group_name=f"{RESOURCE_PREFIX}-cache-sg",
            description="outbound_ivr: ElastiCache Serverless, reachable from the compute host only",
            allow_all_outbound=False,
        )
        cache_sg.add_ingress_rule(
            ec2.Peer.security_group_id(compute_sg.security_group_id),
            ec2.Port.tcp(VALKEY_PORT),
            "Redis-compatible client on the LiveKit host only",
        )

        # ------------------------------------------------------------------
        # Elastic IP: pool slot 1 of the permanent OutboundIvrVoiceStack stack
        # (voice_stack.py), not an EIP this app allocates and owns itself --
        # see this file's module docstring. Read at *deploy* time
        # (ssm.StringParameter.from_string_parameter_name(...).string_value
        # is a CloudFormation dynamic reference CloudFormation resolves at
        # deploy time, not a synth-time context lookup), so `cdk synth`
        # never requires that stack to already exist.
        #
        # The /outbound-ivr/voice/... paths below match voice_stack.py's own
        # SSM_PREFIX exactly -- OutboundIvrVoiceStack is the stack that writes
        # them (see that file's module docstring).
        # ------------------------------------------------------------------
        eip_allocation_id = ssm.StringParameter.from_string_parameter_name(
            self,
            "Eip1AllocationIdParam",
            f"/outbound-ivr/voice/eip/{LIVEKIT_EIP_SLOT}/allocation-id",
        ).string_value
        self.public_ip = ssm.StringParameter.from_string_parameter_name(
            self, "Eip1PublicIpParam", f"/outbound-ivr/voice/eip/{LIVEKIT_EIP_SLOT}/public-ip"
        ).string_value

        # ------------------------------------------------------------------
        # Outbound trunk target: OutboundIvrVoiceStack's own Voice Connector host and
        # termination credentials secret, both read the same deploy-time way
        # as the EIP above. The host is not sensitive (it is the SIP peer
        # address livekit-sip dials, not a credential); the secret's ARN is
        # likewise not sensitive on its own -- only the value behind it is,
        # and that value reaches the trunk-setup container only as an ECS
        # secret, never resolved into this template.
        #
        # Same /outbound-ivr/voice/... paths, deliberately unrenamed -- see the
        # Elastic IP comment just above.
        # ------------------------------------------------------------------
        self.voice_connector_host = ssm.StringParameter.from_string_parameter_name(
            self, "VoiceConnectorOutboundHostParam", "/outbound-ivr/voice/outbound-host"
        ).string_value
        # The caller ID every outbound call presents (OutboundIvrVoiceStack's Chime number).
        caller_id = ssm.StringParameter.from_string_parameter_name(
            self, "VoiceConnectorCallerIdParam", "/outbound-ivr/voice/caller-id"
        ).string_value
        voice_credentials_secret_arn = ssm.StringParameter.from_string_parameter_name(
            self, "VoiceCredentialsSecretArnParam", "/outbound-ivr/voice/credentials-secret-arn"
        ).string_value
        # An imported reference, not a resource this stack creates -- IAM
        # grants against it (below) are scoped to this one ARN, never "*".
        voice_credentials_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "VoiceCredentialsSecret", voice_credentials_secret_arn
        )

        # ------------------------------------------------------------------
        # Secrets: just the ElastiCache AUTH token now -- the Chime Voice
        # Connector's outbound (termination) SIP digest credentials come
        # from OutboundIvrVoiceStack's own secret (imported just above), not one this
        # stack generates. Never a plaintext value in code, in git, or in
        # the task definition's environment -- every one of these three
        # secrets reaches a container only as an ECS `secrets` entry
        # (resolved by the execution role at container start, delivered as
        # an env var the ECS agent sets, never written into the task
        # definition's own template or into CloudWatch Logs).
        # ------------------------------------------------------------------
        redis_auth_secret = secretsmanager.Secret(
            self,
            "RedisAuthSecret",
            secret_name=f"{RESOURCE_PREFIX}/livekit-redis-auth",
            description="Generated Valkey/Redis OSS AUTH token for this stack's ElastiCache Serverless cache",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "default"}),
                generate_string_key="password",
                exclude_punctuation=True,
                password_length=32,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # LiveKit's own API key/secret pair (what livekit-server checks incoming
        # tokens against, and what livekit-sip and the AgentCore Runtime mint
        # tokens with). Generated, never the published "devkey"/"secret" pair
        # that's only for local, non-deployed development against a self-run
        # LiveKit server.
        livekit_keys_secret = secretsmanager.Secret(
            self,
            "LiveKitKeysSecret",
            secret_name=f"{RESOURCE_PREFIX}/livekit-keys",
            description="Generated LiveKit API key/secret pair (not the published devkey/secret dev pair)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"api_key": RESOURCE_PREFIX}),
                generate_string_key="api_secret",
                exclude_punctuation=True,
                password_length=40,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.livekit_api_key = RESOURCE_PREFIX
        # The secret itself (its ARN), not a resolved value: the Outbound IVR Runtime
        # grants its Runtime role read access to this one secret and the
        # container fetches the api_secret field at start -- see
        # agent_runtime_stack.py. Never SecretValue.secrets_manager(...)
        # .unsafe_unwrap() into an environment variable: that bakes a resolved
        # CloudFormation dynamic reference ("{{resolve:secretsmanager...}}")
        # straight into the Runtime's EnvironmentVariables, which is what this
        # app used to do and no longer does.
        self.livekit_keys_secret = livekit_keys_secret

        NagSuppressions.add_resource_suppressions(
            [redis_auth_secret, livekit_keys_secret],
            [
                {
                    "id": "AwsSolutions-SMG4",
                    "reason": (
                        "No automatic rotation: both are credentials a non-AWS "
                        "process (livekit-server's Redis client, LiveKit's own "
                        "token signing key) reads once at container start; "
                        "rotating either means generating a new secret value "
                        "and redeploying this stack, not an automated flow in "
                        "this example. (The Voice Connector's own SIP digest "
                        "credentials are a separate secret in "
                        "OutboundIvrVoiceStack, voice_stack.py.)"
                    ),
                }
            ],
        )

        # ------------------------------------------------------------------
        # ElastiCache Serverless (Valkey): LiveKit's Redis. Always TLS in
        # transit for every Serverless cache, not a flag to set (confirmed
        # against AWS's own ElastiCache docs); AUTH is RBAC, one password-mode
        # user swapped in for the passwordless built-in "default" user (its
        # *user ID* must not be "default", but its *username* must be --
        # ElastiCache rejects a non-"default" username for the built-in user,
        # and a user ID collides across users in the same account/Region if
        # reused).
        # ------------------------------------------------------------------
        cache_kms_key = kms.Key(
            self,
            "CacheKmsKey",
            description="outbound_ivr: ElastiCache Serverless encryption at rest",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
            # 7 days (the minimum AWS allows), not the 30-day default: cdk destroy
            # should finish cleanup promptly for an example readers deploy and
            # tear down repeatedly, and this key protects only this cache's data,
            # not anything that needs a long recovery window.
            pending_window=Duration.days(7),
            alias=f"alias/{RESOURCE_PREFIX}-livekit-cache",
        )

        redis_default_user = elasticache.CfnUser(
            self,
            "RedisDefaultUser",
            engine="valkey",
            user_id=f"{RESOURCE_PREFIX}-default",
            user_name="default",
            access_string="on ~* +@all",
            authentication_mode={
                "Type": "password",
                "Passwords": [
                    SecretValue.secrets_manager(
                        redis_auth_secret.secret_name, json_field="password"
                    ).unsafe_unwrap()
                ],
            },
        )
        redis_user_group = elasticache.CfnUserGroup(
            self,
            "RedisUserGroup",
            engine="valkey",
            user_group_id=f"{RESOURCE_PREFIX}-redis-users",
            user_ids=[redis_default_user.user_id],
        )
        redis_user_group.add_dependency(redis_default_user)

        self.cache = elasticache.CfnServerlessCache(
            self,
            "LiveKitCache",
            engine="valkey",
            serverless_cache_name=f"{RESOURCE_PREFIX}-livekit-cache",
            major_engine_version="8",
            description="LiveKit server's Redis-compatible state store",
            connection_type="vpc",
            security_group_ids=[cache_sg.security_group_id],
            subnet_ids=[s.subnet_id for s in private_subnets],
            user_group_id=redis_user_group.user_group_id,
            kms_key_id=cache_kms_key.key_id,
            cache_usage_limits=elasticache.CfnServerlessCache.CacheUsageLimitsProperty(
                data_storage=elasticache.CfnServerlessCache.DataStorageProperty(
                    unit="GB", maximum=cache_data_storage_max_gb
                ),
                ecpu_per_second=elasticache.CfnServerlessCache.ECPUPerSecondProperty(
                    maximum=cache_ecpu_max_per_second
                ),
            ),
        )
        self.cache.add_dependency(redis_user_group)
        Tags.of(self.cache).add("project", PROJECT_TAG)

        # ------------------------------------------------------------------
        # ECS on EC2: one instance in an Auto Scaling group, wired in as an
        # ASG capacity provider, running a host-network task. Not ECS Managed
        # Instances: AWS-managed instances carry a resource policy that
        # explicitly denies ec2:AssociateAddress, and Chime only accepts calls
        # from the fixed, allowlisted Elastic IP this host must hold.
        # ------------------------------------------------------------------
        cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=f"{RESOURCE_PREFIX}-livekit-cluster",
            vpc=vpc,
            # ENABLED, not ENHANCED. Enhanced bills per custom metric and emits
            # them per task, which on a one-task cluster that replaces its task
            # whenever the host cycles can add up to a meaningful number of metric
            # streams and CloudWatch charges. The cluster, service and task metrics
            # ENABLED gives are what the alarms worth having would use anyway.
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        instance_role = iam.Role(
            self,
            "LiveKitHostRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonEC2ContainerServiceforEC2Role"
                )
            ],
        )
        # No `security_group=` here: this launch template's network interface
        # (below, an L1 override) carries the security group itself --
        # AWS launch templates reject a top-level SecurityGroupIds alongside
        # a network interface that also specifies one.
        launch_template = ec2.LaunchTemplate(
            self,
            "LiveKitHostLaunchTemplate",
            instance_type=ec2.InstanceType("t3.small"),
            machine_image=ecs.EcsOptimizedImage.amazon_linux2023(),
            security_group=compute_sg,
            role=instance_role,
            user_data=ec2.UserData.for_linux(),
            require_imdsv2=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        30, encrypted=True, volume_type=ec2.EbsDeviceVolumeType.GP3
                    ),
                )
            ],
        )
        host_group = autoscaling.AutoScalingGroup(
            self,
            "LiveKitHostGroup",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=public_subnets),
            launch_template=launch_template,
            min_capacity=1,
            max_capacity=1,
            # Without this, EC2 Auto Scaling publishes no group metrics at all, so
            # ops_stack.py's livekit-host-gone alarm (treat_missing_data=BREACHING)
            # has no data point it could ever read as healthy and sits in ALARM
            # from two minutes after every deploy onward, regardless of the host.
            # Group metrics are free.
            group_metrics=[autoscaling.GroupMetrics.all()],
            # Launch template changes (e.g. security groups) replace the host on
            # deploy instead of only affecting the next instance.
            update_policy=autoscaling.UpdatePolicy.rolling_update(
                min_instances_in_service=0, max_batch_size=1
            ),
        )
        NagSuppressions.add_resource_suppressions(
            host_group,
            [
                {
                    "id": "AwsSolutions-AS3",
                    "reason": "One-instance host; no scaling-event notifications needed.",
                }
            ],
        )
        capacity_provider = ecs.AsgCapacityProvider(
            self,
            "LiveKitHostCapacityProvider",
            capacity_provider_name=f"{RESOURCE_PREFIX}-livekit-ec2",
            auto_scaling_group=host_group,
            enable_managed_scaling=False,
            enable_managed_termination_protection=False,
            # ECS drains the task itself, so CDK adds no lifecycle-hook Lambda
            # and SNS topic for draining.
            enable_managed_draining=True,
        )
        cluster.add_asg_capacity_provider(capacity_provider)
        # After add_asg_capacity_provider: it adds the ECS agent statements to
        # this role's default policy, which these suppressions must cover.
        NagSuppressions.add_resource_suppressions(
            instance_role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWS-authored ECS container instance policy, the documented "
                        "way to let the ECS agent register the instance and pull images."
                    ),
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The ECS cluster wiring adds ecs:Poll/StartTelemetrySession "
                        "(scoped by the ecs:cluster condition) and actions with no "
                        "resource-level permissions (ecs:DiscoverPollEndpoint, "
                        "ecr:GetAuthorizationToken, logs:*)."
                    ),
                    "appliesTo": ["Resource::*", "Action::ecs:Submit*"],
                },
            ],
            apply_to_children=True,
        )

        # ------------------------------------------------------------------
        # Instance role: no inbound SSH port or key pair -- debugging goes
        # through ECS Exec (`aws ecs execute-command`) instead, enabled on
        # the service below. No secrets are granted to this role; the three
        # secrets this task needs are granted to the execution role (which
        # resolves ECS `secrets` entries) and, where a container also reads
        # one directly (the AgentCore test's own secret, not this stack's
        # task), to that other role instead.
        # ------------------------------------------------------------------

        task_role = iam.Role(
            self, "TaskRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        )
        # ECS Exec channel permissions: these three actions have no
        # resource-level permissions (AWS always requires Resource: "*"),
        # same as execution_role's ecr:GetAuthorizationToken below.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcsExecChannel",
                actions=[
                    "ssmmessages:CreateControlChannel",
                    "ssmmessages:CreateDataChannel",
                    "ssmmessages:OpenControlChannel",
                    "ssmmessages:OpenDataChannel",
                ],
                resources=["*"],
            )
        )
        NagSuppressions.add_resource_suppressions(
            task_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "ssmmessages:* (ECS Exec's own channel actions) has no "
                        "resource-level permissions; AWS requires "
                        "Resource: '*' for all four, the documented way to "
                        "enable ECS Exec debugging in place of SSH or an SSM "
                        "session on a bare instance."
                    ),
                    "appliesTo": ["Resource::*"],
                }
            ],
            apply_to_children=True,
        )

        log_group = logs.LogGroup(
            self,
            "TaskLogGroup",
            log_group_name=f"/{RESOURCE_PREFIX}/livekit-task",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        execution_role = iam.Role(
            self, "ExecutionRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrAuth",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteTaskLogs",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[log_group.log_group_arn],
            )
        )
        # The execution role resolves every ECS `secrets` entry below; these
        # are the only three secrets this task ever reads, and the only
        # place any of their values exist in plaintext is inside the
        # containers' own environments at runtime -- never in this template.
        redis_auth_secret.grant_read(execution_role)
        voice_credentials_secret.grant_read(execution_role)
        livekit_keys_secret.grant_read(execution_role)
        NagSuppressions.add_resource_suppressions(
            execution_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "ecr:GetAuthorizationToken does not support "
                        "resource-level permissions; AWS requires "
                        "Resource: '*' for this action. The three "
                        "secretsmanager:GetSecretValue grants (via "
                        "grant_read()) are each scoped to exactly one "
                        "secret's own ARN plus its KMS decrypt grant, never "
                        "a wildcard."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:Decrypt*",
                    ],
                }
            ],
            apply_to_children=True,
        )

        task_definition = ecs.TaskDefinition(
            self,
            "TaskDefinition",
            family=f"{RESOURCE_PREFIX}-livekit-task",
            compatibility=ecs.Compatibility.EC2,
            network_mode=ecs.NetworkMode.HOST,
            task_role=task_role,
            execution_role=execution_role,
        )
        NagSuppressions.add_resource_suppressions(
            task_definition,
            [
                {
                    "id": "AwsSolutions-ECS2",
                    "reason": (
                        "The plaintext environment variables here are "
                        "non-sensitive configuration (the Elastic IP, cache "
                        "endpoint, LiveKit API key, Voice Connector host), "
                        "not secrets. Actual credentials (the Redis AUTH "
                        "token, the Voice Connector's SIP digest "
                        "credentials, the LiveKit API secret) go through "
                        "the `secrets` field from Secrets Manager."
                    ),
                }
            ],
        )

        shared_config_volume_name = "livekit-config"
        task_definition.add_volume(name=shared_config_volume_name)

        # ------------------------------------------------------------------
        # Init container: essential=False, waits until the host's own public
        # IP (read from IMDSv2, a single-hop call -- the host network mode
        # this task runs in means the container shares the host's own network
        # namespace, so IMDSv2 is the host's, not a separate ENI's) equals the
        # OutboundIvrVoiceStack pool's slot-1 Elastic IP -- the EIP associator
        # Lambda below attaches it asynchronously, off the same "task reached
        # RUNNING" event that starts this container, so there is no ordering
        # guarantee it has landed yet. If the IP never matches before the
        # timeout, this container exits non-zero;
        # livekit-server/livekit-sip's SUCCESS dependency on it is then
        # never satisfied, and the task fails -- the ECS service scheduler
        # replaces it with a fresh task (and, eventually, a fresh instance),
        # which gives the associator Lambda another chance to attach the
        # EIP before this container's next attempt.
        #
        # Once the IP matches, this container renders livekit.yaml and
        # the SIP config.yaml onto the shared task volume: node_ip is the Elastic IP,
        # Redis is the ElastiCache endpoint with TLS and the generated AUTH
        # secret, delivered here only as the ECS secret REDIS_PASSWORD.
        # ------------------------------------------------------------------
        config_renderer = task_definition.add_container(
            "config-renderer",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/docker/library/alpine:3.20"),
            essential=False,
            cpu=128,
            memory_reservation_mib=64,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="config-renderer", log_group=log_group),
            environment={
                "EXPECTED_PUBLIC_IP": self.public_ip,
                "CACHE_ENDPOINT": self.cache.attr_endpoint_address,
                "CACHE_PORT": self.cache.attr_endpoint_port,
                "LIVEKIT_API_KEY": self.livekit_api_key,
                "WEBRTC_PORT_RANGE_START": str(WEBRTC_UDP_PORT_RANGE[0]),
                "WEBRTC_PORT_RANGE_END": str(WEBRTC_UDP_PORT_RANGE[1]),
                "SIP_PORT": str(SIP_TLS_PORT),
                "RTP_PORT_RANGE": f"{SIP_RTP_PORT_RANGE[0]}-{SIP_RTP_PORT_RANGE[1]}",
            },
            secrets={
                "REDIS_PASSWORD": ecs.Secret.from_secrets_manager(
                    redis_auth_secret, field="password"
                ),
                "LIVEKIT_API_SECRET": ecs.Secret.from_secrets_manager(
                    livekit_keys_secret, field="api_secret"
                ),
            },
            entry_point=["sh", "-c"],
            command=[self._render_configs_script()],
        )
        config_renderer.add_mount_points(
            ecs.MountPoint(
                container_path="/shared",
                read_only=False,
                source_volume=shared_config_volume_name,
            )
        )

        livekit_server_container = task_definition.add_container(
            "livekit-server",
            image=ecs.ContainerImage.from_registry(livekit_server_image),
            essential=True,
            cpu=512,
            memory_reservation_mib=896,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="livekit-server", log_group=log_group),
            command=["--config", "/shared/livekit.yaml"],
            # Without a health check, a livekit-server that hangs without exiting
            # (stuck retrying ElastiCache, say) fails every call silently: the ASG
            # only checks the EC2 instance and ECS only notices a process that
            # actually dies. An HTTP request answers the question a port check
            # cannot, which is whether the server is still serving.
            #
            # Command verified against livekit/livekit-server:v1.13.10 itself:
            # wget is present (curl and bash are not), /bin/sh exists, and GET /
            # returns "OK". Do not swap in curl or bash here.
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    f"wget -q -O- -T 2 http://127.0.0.1:{LIVEKIT_SIGNALING_PORT}/ >/dev/null || exit 1",
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                # The config-renderer sidecar has to finish first, and the server
                # reads its config and connects to the cache before it listens.
                start_period=Duration.seconds(60),
            ),
        )
        livekit_server_container.add_mount_points(
            ecs.MountPoint(
                container_path="/shared",
                read_only=True,
                source_volume=shared_config_volume_name,
            )
        )
        livekit_server_container.add_container_dependencies(
            ecs.ContainerDependency(
                container=config_renderer, condition=ecs.ContainerDependencyCondition.SUCCESS
            )
        )
        livekit_server_container.add_port_mappings(
            ecs.PortMapping(container_port=LIVEKIT_SIGNALING_PORT, protocol=ecs.Protocol.TCP),
            ecs.PortMapping(container_port=LIVEKIT_TCP_FALLBACK_PORT, protocol=ecs.Protocol.TCP),
        )

        livekit_sip_container = task_definition.add_container(
            "livekit-sip",
            image=ecs.ContainerImage.from_registry(livekit_sip_image),
            essential=True,
            cpu=256,
            memory_reservation_mib=384,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="livekit-sip", log_group=log_group),
            # A TCP check, not HTTP: this process speaks SIP, and its only useful
            # liveness signal is that it is still accepting on the signaling port.
            #
            # CMD, not CMD-SHELL, and bash by name. Verified against
            # livekit/sip:v1.19.0: it has no wget, no curl and no nc, its /bin/sh
            # is dash (no /dev/tcp), and bash is present with /dev/tcp compiled
            # in. CMD-SHELL would run dash and fail every check, which ECS would
            # read as a dead container and restart forever.
            health_check=ecs.HealthCheck(
                command=["CMD", "bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{SIP_TLS_PORT}"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                # Waits on the config sidecar and on livekit-server.
                start_period=Duration.seconds(90),
            ),
        )
        # The image's entrypoint reads /sip/config.yaml and ignores
        # SIP_CONFIG_FILE, so the shared volume is mounted there.
        livekit_sip_container.add_mount_points(
            ecs.MountPoint(
                container_path="/sip",
                read_only=True,
                source_volume=shared_config_volume_name,
            )
        )
        livekit_sip_container.add_container_dependencies(
            ecs.ContainerDependency(
                container=config_renderer, condition=ecs.ContainerDependencyCondition.SUCCESS
            ),
            ecs.ContainerDependency(
                container=livekit_server_container,
                condition=ecs.ContainerDependencyCondition.START,
            ),
        )
        livekit_sip_container.add_port_mappings(
            ecs.PortMapping(container_port=SIP_TLS_PORT, protocol=ecs.Protocol.TCP),
        )

        # ------------------------------------------------------------------
        # Trunk-setup container: essential=False, starts once livekit-server
        # has started, waits for it to accept connections, then registers
        # livekit-sip's one outbound trunk against OutboundIvrVoiceStack's Voice
        # Connector (point (d)) -- the same `livekit.api` call shape
        # ../../sip_setup.py already uses and has run successfully against a
        # real (local) LiveKit server. Not idempotent (same caveat as the
        # EC2-era version of this stack): a replaced task re-runs this and
        # would create a second, duplicate trunk unless an existing-trunk
        # check is added first -- a Day 1 follow-up, not done in this pass.
        # ------------------------------------------------------------------
        trunk_setup_container = task_definition.add_container(
            "trunk-setup",
            image=ecs.ContainerImage.from_registry(
                "public.ecr.aws/docker/library/python:3.12-slim"
            ),
            essential=False,
            cpu=128,
            memory_reservation_mib=192,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="trunk-setup", log_group=log_group),
            environment={
                "VOICE_CONNECTOR_HOST": self.voice_connector_host,
                "CALLER_ID": caller_id,
                "SIP_TLS_PORT": str(SIP_TLS_PORT),
                "LIVEKIT_API_KEY": self.livekit_api_key,
            },
            secrets={
                "LIVEKIT_API_SECRET": ecs.Secret.from_secrets_manager(
                    livekit_keys_secret, field="api_secret"
                ),
                "SIP_USERNAME": ecs.Secret.from_secrets_manager(
                    voice_credentials_secret, field="username"
                ),
                "SIP_PASSWORD": ecs.Secret.from_secrets_manager(
                    voice_credentials_secret, field="password"
                ),
            },
            entry_point=["sh", "-c"],
            command=[self._trunk_setup_script()],
        )
        trunk_setup_container.add_container_dependencies(
            ecs.ContainerDependency(
                container=livekit_server_container,
                condition=ecs.ContainerDependencyCondition.START,
            )
        )

        service_name = f"{RESOURCE_PREFIX}-livekit-service"
        service = ecs.CfnService(
            self,
            "LiveKitService",
            service_name=service_name,
            cluster=cluster.cluster_name,
            task_definition=task_definition.task_definition_arn,
            desired_count=1,
            enable_execute_command=True,
            capacity_provider_strategy=[
                ecs.CfnService.CapacityProviderStrategyItemProperty(
                    capacity_provider=capacity_provider.capacity_provider_name,
                    weight=1,
                )
            ],
            deployment_configuration=ecs.CfnService.DeploymentConfigurationProperty(
                minimum_healthy_percent=0,
                maximum_percent=100,
                # Fail the deploy when tasks cannot start, instead of
                # CloudFormation waiting hours for a service that never stabilizes.
                deployment_circuit_breaker=ecs.CfnService.DeploymentCircuitBreakerProperty(
                    enable=True, rollback=False
                ),
            ),
        )
        service.node.add_dependency(cluster)

        # Published for ops_stack.py's alarms. The cluster and service names are
        # literals it could hardcode; the ASG's is generated, so it has to come
        # from here.
        self.host_group_name = host_group.auto_scaling_group_name
        self.cluster_name = cluster.cluster_name
        self.service_name = service_name

        # ------------------------------------------------------------------
        # Elastic IP re-association: ECS Managed Instances gives AWS the EC2
        # instance's lifecycle, so there is no synth-time instance ID to
        # attach the pool's slot-1 Elastic IP to declaratively. This Lambda
        # fires off the same ECS "Task State Change" -> RUNNING event the
        # init container above also starts racing against (see that
        # container's own comment for the ordering this does not guarantee).
        # It also points the private DNS record at the host (module docstring),
        # and, invoked as a CloudFormation custom resource, deletes that record
        # when the stack is deleted.
        # ------------------------------------------------------------------
        eip_associator = _lambda.Function(
            self,
            "GatewayEipAssociator",
            runtime=_lambda.Runtime.PYTHON_3_14,
            handler="index.handler",
            timeout=Duration.seconds(30),
            # Explicit group rather than `log_retention`, which would add a
            # singleton custom-resource Lambda holding logs:PutRetentionPolicy on
            # "*" (this stack's own test forbids a bare wildcard). These logs are
            # the only record of why the host came back without its Elastic IP or
            # its DNS record, which is the failure the runbook sends an operator
            # looking for, and the implicit group never expires.
            log_group=logs.LogGroup(
                self,
                "GatewayEipAssociatorLogGroup",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                "ALLOCATION_ID": eip_allocation_id,
                "HOSTED_ZONE_ID": zone.hosted_zone_id,
                "RECORD_NAME": LIVEKIT_RECORD_NAME,
            },
            code=_lambda.Code.from_inline(
                """
import json
import os
import urllib.request

import boto3

ecs_client = boto3.client("ecs")
ec2_client = boto3.client("ec2")
route53_client = boto3.client("route53")


def _record_change(action, ip):
    return {
        "Action": action,
        "ResourceRecordSet": {
            "Name": os.environ["RECORD_NAME"],
            "Type": "A",
            "TTL": 30,
            "ResourceRecords": [{"Value": ip}],
        },
    }


def _delete_record():
    found = route53_client.list_resource_record_sets(
        HostedZoneId=os.environ["HOSTED_ZONE_ID"],
        StartRecordName=os.environ["RECORD_NAME"],
        StartRecordType="A",
        MaxItems="1",
    )["ResourceRecordSets"]
    for rrset in found:
        if rrset["Name"].rstrip(".") == os.environ["RECORD_NAME"] and rrset["Type"] == "A":
            route53_client.change_resource_record_sets(
                HostedZoneId=os.environ["HOSTED_ZONE_ID"],
                ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": rrset}]},
            )


def _cfn_respond(event, status, reason=""):
    body = json.dumps({
        "Status": status,
        "Reason": reason[:200],
        "PhysicalResourceId": "livekit-dns-record",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
    }).encode()
    request = urllib.request.Request(event["ResponseURL"], data=body, method="PUT", headers={"Content-Type": ""})
    urllib.request.urlopen(request, timeout=10)


def handler(event, _context):
    # CloudFormation custom resource: only Delete does anything (removes the record
    # this Lambda manages, so the hosted zone can be deleted).
    if "RequestType" in event:
        try:
            if event["RequestType"] == "Delete":
                _delete_record()
            _cfn_respond(event, "SUCCESS")
        except Exception as exc:
            _cfn_respond(event, "FAILED", str(exc))
        return

    detail = event.get("detail", {})
    container_instance_arn = detail.get("containerInstanceArn")
    cluster_arn = detail.get("clusterArn")
    if not container_instance_arn or not cluster_arn:
        return

    described = ecs_client.describe_container_instances(
        cluster=cluster_arn, containerInstances=[container_instance_arn]
    )
    instances = described.get("containerInstances", [])
    if not instances:
        return
    ec2_instance_id = instances[0]["ec2InstanceId"]

    ec2_client.associate_address(
        InstanceId=ec2_instance_id,
        AllocationId=os.environ["ALLOCATION_ID"],
        AllowReassociation=True,
    )

    reservations = ec2_client.describe_instances(InstanceIds=[ec2_instance_id])["Reservations"]
    private_ip = reservations[0]["Instances"][0]["PrivateIpAddress"]
    route53_client.change_resource_record_sets(
        HostedZoneId=os.environ["HOSTED_ZONE_ID"],
        ChangeBatch={"Changes": [_record_change("UPSERT", private_ip)]},
    )
"""
            ),
        )
        eip_associator.add_to_role_policy(
            iam.PolicyStatement(
                sid="DescribeOwnClusterContainerInstances",
                actions=["ecs:DescribeContainerInstances"],
                resources=["*"],
                conditions={"ArnEquals": {"ecs:cluster": cluster.cluster_arn}},
            )
        )
        eip_associator.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReassociateLiveKitEip",
                actions=["ec2:AssociateAddress"],
                resources=[
                    f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/*",
                    f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:elastic-ip/{eip_allocation_id}",
                ],
            )
        )
        eip_associator.add_to_role_policy(
            iam.PolicyStatement(
                sid="DescribeInstancesForPrivateIp",
                actions=["ec2:DescribeInstances"],
                resources=["*"],
            )
        )
        eip_associator.add_to_role_policy(
            iam.PolicyStatement(
                sid="ManageLiveKitDnsRecord",
                actions=["route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets"],
                resources=[zone.hosted_zone_arn],
            )
        )
        # Deletes the record on stack deletion (the handler's custom-resource branch),
        # before CloudFormation deletes the zone it lives in.
        dns_cleanup = CustomResource(
            self,
            "LiveKitDnsRecordCleanup",
            service_token=eip_associator.function_arn,
            properties={"HostedZoneId": zone.hosted_zone_id},
        )
        dns_cleanup.node.add_dependency(zone)
        NagSuppressions.add_resource_suppressions(
            eip_associator,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWSLambdaBasicExecutionRole (CloudWatch Logs only) "
                        "auto-attached by the Lambda construct's default "
                        "role; no customer-authored equivalent is simpler."
                    ),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "ecs:DescribeContainerInstances has no "
                        "resource-level permissions (restricted via the "
                        "ecs:cluster condition instead). "
                        "ec2:AssociateAddress targets 'instance/*' because "
                        "the Auto Scaling group chooses the EC2 instance ID "
                        "at launch time; it cannot be known at synth time. The elastic-ip resource is "
                        "scoped exactly, to OutboundIvrVoiceStack's pool slot 1 "
                        "allocation ID resolved from SSM at deploy time."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        f"Resource::arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/*",
                    ],
                },
            ],
            apply_to_children=True,
        )

        task_running_rule = events.Rule(
            self,
            "TaskRunningRule",
            description="Re-point the LiveKit Elastic IP when the task (re)lands on a new host",
            event_pattern=events.EventPattern(
                source=["aws.ecs"],
                detail_type=["ECS Task State Change"],
                detail={
                    "clusterArn": [cluster.cluster_arn],
                    # PENDING: the task is on its instance, but the init
                    # container is still waiting for this Elastic IP, so the
                    # task cannot reach RUNNING until this Lambda has run.
                    "lastStatus": ["PENDING", "RUNNING"],
                    "group": [f"service:{service_name}"],
                },
            ),
        )
        task_running_rule.add_target(targets.LambdaFunction(eip_associator))

        # ------------------------------------------------------------------
        # Test destination numbers: never a literal in code or in git -- only
        # a context value. Stored in SSM so an operator (or a dial script)
        # reads them instead of re-typing them; defaults are obviously-fake
        # placeholders, not real numbers.
        # ------------------------------------------------------------------
        test_cell_number = self.node.try_get_context("testCellNumber") or "+15555550100"
        office_number = self.node.try_get_context("officeNumber") or "+15555550101"
        ssm.StringParameter(
            self,
            "TestDestinationNumbers",
            parameter_name=f"/{RESOURCE_PREFIX}/test-destination-numbers",
            string_value=json.dumps({"cell": test_cell_number, "office": office_number}),
            description=(
                "E.164 numbers for manual test calls; set only via "
                "-c testCellNumber=... -c officeNumber=... at synth time, never "
                "hardcoded, so no real phone number is ever committed to git."
            ),
        )

        Tags.of(self).add("project", PROJECT_TAG)

        CfnOutput(self, "LiveKitPublicIp", value=self.public_ip)
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "VoiceConnectorOutboundHost", value=self.voice_connector_host)
        CfnOutput(self, "CacheEndpoint", value=self.cache.attr_endpoint_address)

    def _render_configs_script(self) -> str:
        """The init container's own entrypoint (shell, run via `sh -c`): installs
        curl, waits (IMDSv2, a single-hop call -- host network mode means the
        container shares the host's own network namespace) until the host's own
        public IP matches the OutboundIvrVoiceStack pool's slot-1 Elastic IP
        (`EXPECTED_PUBLIC_IP`), then renders livekit.yaml and the SIP config.yaml onto the
        shared task volume. Times out (exit 1) after a bounded number of
        attempts rather than looping forever -- see this stack's own comment on
        this container for what that failure does to the task.
        """
        return r"""
set -eu
apk add --no-cache curl >/dev/null

attempt=0
max_attempts=60
while :; do
  TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" || true)
  ACTUAL_IP=$(curl -s -H "X-aws-ec2-metadata-token: ${TOKEN}" \
    "http://169.254.169.254/latest/meta-data/public-ipv4" || true)
  if [ "${ACTUAL_IP}" = "${EXPECTED_PUBLIC_IP}" ]; then
    break
  fi
  attempt=$((attempt + 1))
  if [ "${attempt}" -ge "${max_attempts}" ]; then
    echo "timed out waiting for public IP ${EXPECTED_PUBLIC_IP}, saw ${ACTUAL_IP}" >&2
    exit 1
  fi
  echo "waiting for the Elastic IP to associate (attempt ${attempt}/${max_attempts})..."
  sleep 5
done

cat > /shared/livekit.yaml <<EOF
port: 7880
rtc:
  tcp_port: 7881
  port_range_start: ${WEBRTC_PORT_RANGE_START}
  port_range_end: ${WEBRTC_PORT_RANGE_END}
  use_external_ip: false
  node_ip: ${EXPECTED_PUBLIC_IP}
  # Also offer the private IP: the AgentCore Runtime sits in isolated subnets
  # with no route to the public Elastic IP.
  advertise_internal_ip: true
redis:
  address: ${CACHE_ENDPOINT}:${CACHE_PORT}
  username: default
  password: ${REDIS_PASSWORD}
  use_tls: true
keys:
  ${LIVEKIT_API_KEY}: ${LIVEKIT_API_SECRET}
EOF

# livekit/sip's entrypoint is fixed to --config=/sip/config.yaml; the sip
# container mounts this volume at /sip.
cat > /shared/config.yaml <<EOF
api_key: ${LIVEKIT_API_KEY}
api_secret: ${LIVEKIT_API_SECRET}
ws_url: ws://localhost:7880
redis:
  address: ${CACHE_ENDPOINT}:${CACHE_PORT}
  username: default
  password: ${REDIS_PASSWORD}
  use_tls: true
sip_port: ${SIP_PORT}
rtp_port: ${RTP_PORT_RANGE}
use_external_ip: true
# The phone network's hang-up (BYE) does not always reach this bridge, but the far
# end's audio stops at once. Ten seconds of no audio ends the call (the default is two
# minutes); even hold music keeps audio flowing.
media_timeout: 10s
media_timeout_initial: 30s
EOF
echo "rendered livekit.yaml and config.yaml (SIP)"
"""

    def _trunk_setup_script(self) -> str:
        """The trunk-setup container's own entrypoint: waits for livekit-server to
        accept connections (`docker compose up -d`'s own equivalent wait, now a
        plain TCP probe against the host-network-shared port), installs
        `livekit-api` pinned to the version the Runtime uses (pyproject.toml's
        livekit extra; bump both together), then registers livekit-sip's one outbound trunk against
        OutboundIvrVoiceStack's Voice Connector -- TLS signaling, SRTP media required, the
        digest credentials read back out of the environment (never resolved into
        this template; see `test_outbound_trunk_requires_tls_and_srtp...` for the
        regression guard on that).
        """
        return r"""
set -eu
pip install --quiet livekit-api==1.2.2

attempt=0
while :; do
  if python3 -c "import socket; s=socket.create_connection(('localhost', 7880), timeout=2); s.close()" 2>/dev/null; then
    break
  fi
  attempt=$((attempt + 1))
  if [ "${attempt}" -ge 30 ]; then
    echo "timed out waiting for livekit-server to accept connections" >&2
    exit 1
  fi
  sleep 2
done

cat > /tmp/create_outbound_trunk.py <<'PYEOF'
import asyncio
import os

from livekit import api


async def main() -> None:
    lkapi = api.LiveKitAPI(
        url="ws://localhost:7880",
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        # Replace, not add: every task start runs this, and duplicates would pile up.
        existing = await lkapi.sip.list_sip_outbound_trunk(api.ListSIPOutboundTrunkRequest())
        for trunk in existing.items:
            if trunk.name == "outbound-ivr-chime-outbound":
                await lkapi.sip.delete_sip_trunk(api.DeleteSIPTrunkRequest(sip_trunk_id=trunk.sip_trunk_id))
        await lkapi.sip.create_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(
                trunk=api.SIPOutboundTrunkInfo(
                    name="outbound-ivr-chime-outbound",
                    address=f'{os.environ["VOICE_CONNECTOR_HOST"]}:{os.environ["SIP_TLS_PORT"]}',
                    transport=api.SIPTransport.SIP_TRANSPORT_TLS,
                    media_encryption=api.SIPMediaEncryption.SIP_MEDIA_ENCRYPT_REQUIRE,
                    # The caller ID: Chime only accepts its own numbers.
                    numbers=[os.environ["CALLER_ID"]],
                    auth_username=os.environ["SIP_USERNAME"],
                    auth_password=os.environ["SIP_PASSWORD"],
                )
            )
        )
    finally:
        await lkapi.aclose()


asyncio.run(main())
PYEOF
python3 /tmp/create_outbound_trunk.py
"""
