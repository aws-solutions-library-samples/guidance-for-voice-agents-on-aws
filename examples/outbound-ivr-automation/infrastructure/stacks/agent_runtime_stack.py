"""outbound-ivr-agent-runtime: the Outbound IVR Runtime -- an `agentcore.Runtime` in VPC
mode (`RuntimeNetworkConfiguration.using_vpc(...)`, private subnets, IAM/SigV4 inbound,
the default when no `authorizer_configuration` is given), pointed at the real agent
entrypoint `src/agentcore/livekit_app.py`. The entrypoint runs the Outbound IVR agent as
a background task and returns from the invoke immediately (the SDK's own
async-task/HealthyBusy ping mechanism keeps the session alive for the rest of the call).

**Build context.** `agentcore.AgentRuntimeArtifact.from_asset(directory, file=...)`
builds `file` relative to `directory`. This stack passes `directory=runtimeImageDir`
(context value, default `".."` -- the example root, since `cdk synth`/`cdk deploy`
always run from this app's own `infrastructure/` working directory) with an explicit
`file="src/agentcore/Dockerfile"`, so the Dockerfile's own `COPY src`/
`COPY pyproject.toml` lines resolve against the example root, where those paths
actually live.
"""

from __future__ import annotations

from aws_cdk import Annotations, Duration, NestedStack, Stack, Tags
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from cdk_nag import NagSuppressions
from constructs import Construct

from .constants import (
    DEFAULT_SONIC_MODEL,
    DEFAULT_TRUNK_NAME,
    PROJECT_TAG,
    REGION,
    RESOURCE_PREFIX,
)

# Named once: the execution role's trust policy scopes aws:SourceArn to it, and
# the role is created before the runtime, so this has to be a literal. Deliberately
# not derived from RESOURCE_PREFIX ("outbound-ivr"): AgentCore Runtime names only
# accept letters, digits and underscores (no hyphens).
RUNTIME_NAME = "outbound_ivr_agent"


class AgentRuntimeNestedStack(NestedStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        private_subnets: ec2.SubnetSelection,
        events_table: dynamodb.Table,
        cases_table: dynamodb.Table,
        offices_table: dynamodb.Table,
        artifacts_bucket: s3.Bucket,
        handoff_table: dynamodb.Table,
        evidence_number: str | None,
        livekit_host: str,
        livekit_api_key: str,
        livekit_api_secret_arn: str,
        gateway_url: str,
        office_phone_number: str | None = None,
        monitor_bucket: s3.Bucket | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        runtime_image_dir = self.node.try_get_context("runtimeImageDir") or ".."
        sonic_model = self.node.try_get_context("sonicModel") or DEFAULT_SONIC_MODEL
        # The real office number -- OutboundIvrOfficeStack's claimed DID, or its
        # documented `+15555550101` placeholder when none is claimed -- read out
        # of SSM and handed in by the caller wins when there is one to give.
        # outbound_ivr_stack.py always has one now: office_stack.py's OutboundIvrOfficeStack
        # writes it unconditionally, every deploy, and outbound_ivr_stack.py reads it
        # with no CloudFormation dependency on that stack (its own module
        # docstring's SSM contract), so there is no ordering constraint left
        # that would keep this parameter from reaching AgentRuntime on the very
        # first build. Context officeNumber (or the placeholder default below)
        # is the fallback for a caller that does not go through outbound_ivr_stack.py
        # at all -- a standalone synth of this nested stack on its own, or any
        # future one, with no office stack deployed to read from.
        office_number = (
            office_phone_number or self.node.try_get_context("officeNumber") or "+15555550101"
        )
        trunk_name = self.node.try_get_context("trunkName") or DEFAULT_TRUNK_NAME
        idle_timeout_minutes = int(self.node.try_get_context("idleTimeoutMinutes") or 30)
        max_lifetime_hours = int(self.node.try_get_context("maxLifetimeHours") or 8)
        sonic_model_arn = f"arn:aws:bedrock:{REGION}::foundation-model/{sonic_model}"

        # ------------------------------------------------------------------
        # Execution role: exactly the grants the shared contract lists --
        # Nova Sonic bidirectional streaming on the one model this Runtime
        # is told to use, S3 writes on the two object-key prefixes it
        # writes (case record, recording), one DynamoDB write apiece on the
        # events table (per-call event log) and the handoff table (the
        # escalation stack's handoff-code row), read access to the one
        # LiveKit secret it needs to join the room, and direct (read-only)
        # access to the cases/offices tables -- see ReadCaseAndOffice and
        # RecordCaseOutcome below. That direct read path is unchanged by
        # OUTBOUND_IVR_TOOLS=gateway: GatewayBackend.case_for_prompt still reads
        # the case straight out of DynamoDB with this role's own GetItem,
        # not through the Gateway; only the mutating tool calls
        # (record_item/escalate/complete/transfer_to_team) go through
        # gateway_stack.py's Lambda target and its own, separate role.
        # ------------------------------------------------------------------
        # Bedrock refuses InvokeModelWithBidirectionalStream (an AccessDenied with an
        # empty message) unless the AgentCore execution role's name starts with
        # this prefix, whatever its policy says.
        self.execution_role = iam.Role(
            self,
            "AgentExecutionRole",
            role_name=f"AmazonBedrockAgentCoreSDKRuntime-{REGION}-{RESOURCE_PREFIX}-agent",
            # Confused-deputy prevention, the pair AWS documents for exactly this
            # role (runtime-permissions.html "AgentCore Runtime trust policy" and
            # runtime-security-best-practices.html "Confused deputy prevention").
            # Without them, any caller who can get the AgentCore service principal
            # to act on their behalf -- in any account -- can assume a role that
            # reads this one's cases table and invokes Nova Sonic on its bill.
            #
            # Scoped to this runtime's own name rather than the account-wide ":*"
            # AWS's example uses. There is no dependency cycle in doing that: the
            # runtime NAME is a literal in this file, so the ARN prefix is known
            # before the runtime exists, even though its ARN is not.
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{REGION}:{self.account}"
                            f":runtime/{RUNTIME_NAME}*"
                        )
                    },
                },
            ),
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeNovaSonic",
                # The bidirectional stream is authorized as bedrock:InvokeModel
                # (Bedrock API reference), not only its own action name.
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:InvokeModelWithBidirectionalStream",
                ],
                resources=[sonic_model_arn],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteCaseAndRecordingArtifacts",
                actions=["s3:PutObject"],
                resources=[
                    artifacts_bucket.arn_for_objects("cases/*"),
                    artifacts_bucket.arn_for_objects("recordings/*"),
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteCallEvents",
                actions=["dynamodb:PutItem"],
                resources=[events_table.table_arn],
            )
        )
        # The events table and the artifacts bucket are encrypted with the data
        # stack's customer-managed key; writes need it (DynamoDB and S3 call KMS
        # on the caller's behalf, so no KMS endpoint is needed).
        data_key = events_table.encryption_key
        if data_key is not None:
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="UseDataKey",
                    actions=[
                        "kms:Encrypt",
                        "kms:Decrypt",
                        "kms:GenerateDataKey",
                        "kms:DescribeKey",
                    ],
                    resources=[data_key.key_arn],
                )
            )
        # At call start: read the case the dashboard started, and its office.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadCaseAndOffice",
                actions=["dynamodb:GetItem"],
                resources=[cases_table.table_arn, offices_table.table_arn],
            )
        )
        # At call end: read the call's handoff event, close out the case item.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="RecordCaseOutcome",
                actions=["dynamodb:Query", "dynamodb:UpdateItem"],
                resources=[events_table.table_arn, cases_table.table_arn],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteHandoffCode",
                actions=["dynamodb:PutItem"],
                resources=[handoff_table.table_arn],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadLiveKitApiSecret",
                actions=["secretsmanager:GetSecretValue"],
                resources=[livekit_api_secret_arn],
            )
        )
        # Monitor: src/outbound_ivr/voice/monitor.py's MonitorPublisher writes
        # segments and polls (HeadObject) the watch markers -- optional,
        # only wired when outbound_ivr_stack.py passes the bucket (see this
        # stack's own __init__ signature and private-monitor-s3-spec.md's
        # "Runtime IAM"). Scoped to this one bucket and its own `seg/`/
        # `watch/` key prefixes, never a wildcard across buckets. No
        # separate KMS grant: the bucket shares the data stack's own CMK
        # (data_key above), and UseDataKey already covers
        # GenerateDataKey/Decrypt on it.
        if monitor_bucket is not None:
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="WriteMonitorSegments",
                    actions=["s3:PutObject"],
                    resources=[monitor_bucket.arn_for_objects("*/seg/*")],
                )
            )
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadMonitorWatchMarkers",
                    # HeadObject is authorized as s3:GetObject.
                    actions=["s3:GetObject"],
                    resources=[monitor_bucket.arn_for_objects("*/watch/*")],
                )
            )
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="DeleteMonitorObjectsOnClose",
                    actions=["s3:DeleteObject"],
                    resources=[
                        monitor_bucket.arn_for_objects("*/seg/*"),
                        monitor_bucket.arn_for_objects("*/watch/*"),
                    ],
                )
            )
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ListMonitorBucketForClose",
                    actions=["s3:ListBucket"],
                    resources=[monitor_bucket.bucket_arn],
                )
            )
        NagSuppressions.add_resource_suppressions(
            self.execution_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Every statement above is scoped to one object-key "
                        "prefix, one table, or one secret's own ARN -- "
                        "never a wildcard. The CloudWatch Logs and "
                        "workload-identity-directory grants (prefix-scoped, "
                        "not resource-exact) and the X-Ray 'Resource::*' "
                        "statement are added automatically by the Runtime "
                        "L2 construct for observability and tracing "
                        "(tracing_enabled=True) using AWS's own documented "
                        "resource patterns."
                    ),
                    "appliesTo": [
                        f"Resource::<{Stack.of(self).get_logical_id(artifacts_bucket.node.default_child)}.Arn>/cases/*",
                        f"Resource::<{Stack.of(self).get_logical_id(artifacts_bucket.node.default_child)}.Arn>/recordings/*",
                        "Resource::*",
                        {"regex": "/^Resource::arn:.*:logs:.*:log-group:.*$/"},
                        {
                            "regex": "/^Resource::arn:.*:bedrock-agentcore:.*:workload-identity-directory\\/.*$/"
                        },
                        # The Monitor bucket's own ARN, cross-nested-stack
                        # (MonitorNestedStack) -- a plain f-string with the
                        # raw token wouldn't match cdk-nag's own rendering of
                        # that cross-stack GetAtt reference.
                        {"regex": "/^Resource::.*\\/seg\\/\\*$/"},
                        {"regex": "/^Resource::.*\\/watch\\/\\*$/"},
                    ],
                }
            ],
            apply_to_children=True,
        )

        # ------------------------------------------------------------------
        # Runtime: the entrypoint joins/creates a LiveKit room named after
        # call_id, runs agent.handle_call via LiveKitTransport, and dials
        # the office through the outbound trunk -- see
        # src/agentcore/livekit_app.py. Fires its call-handling work as a
        # background task and returns right away (the SDK's own
        # async-task/HealthyBusy ping mechanism keeps the session alive for
        # the rest of the call) -- entirely the entrypoint's own code, nothing
        # this stack's IAM/env wiring needs to know about beyond what env vars
        # to set.
        # ------------------------------------------------------------------
        agent_runtime_artifact = agentcore.AgentRuntimeArtifact.from_asset(
            runtime_image_dir, file="src/agentcore/Dockerfile"
        )

        # ------------------------------------------------------------------
        # Call content in telemetry: ONE switch, `-c traceCallContent=false`.
        #
        # It used to be two half-switches. `OUTBOUND_IVR_TRACE_CALL_CONTENT` gated
        # only this repo's own transcript attribute, while
        # `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT` was hardcoded "true" and kept
        # every prompt and model response on the platform's spans regardless.
        # Setting traceCallContent=false therefore looked like it stopped call
        # content reaching telemetry and did not.
        #
        # "true" KEEPS prompt and response text, despite the variable's name:
        # "AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true keeps prompt and response
        # text in the spans, which Agent traces and evaluators read" (omni-send-
        # ai-agent-telemetry.html, "AgentCore"). That page documents no value
        # for the other direction, so turning content off OMITS the variable
        # rather than setting it "false" -- an undocumented value is a guess,
        # and the guess that leaks is the wrong one to make.
        #
        # Default is still on, because the six online evaluators below read that
        # content and a demo with no scores is a broken demo. The call data here
        # is synthetic. Flip this to false before pointing this example at real
        # patient data -- the warning below says so at deploy time, so nobody has
        # to find this comment to know that.
        # ------------------------------------------------------------------
        trace_call_content = (
            str(self.node.try_get_context("traceCallContent") or "true").lower() != "false"
        )

        # Six LLM judges per call, each a Bedrock invocation, times every call.
        # Fine at demo volume and an obvious cost trap at any other, which is why
        # it is a context value here rather than a literal. Default stays 100
        # so a demo keeps scoring every call.
        # `or 100` would swallow a context value of 0, which is exactly the value
        # worth rejecting loudly.
        _sampling_context = self.node.try_get_context("evaluationSamplingPercentage")
        evaluation_sampling_percentage = (
            100 if _sampling_context is None else int(_sampling_context)
        )
        if not 1 <= evaluation_sampling_percentage <= 100:
            raise ValueError(
                "context evaluationSamplingPercentage must be between 1 and 100, got "
                f"{evaluation_sampling_percentage}"
            )
        content_extraction_env = (
            {"AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT": "true"} if trace_call_content else {}
        )
        # Monitor env var: only when outbound_ivr_stack.py passes the bucket (same
        # conditional as the IAM grants above). src/outbound_ivr/voice/monitor.py's
        # MonitorPublisher is a complete no-op when OUTBOUND_IVR_MONITOR_BUCKET is
        # unset, so omitting it here is how this stack disables Monitor
        # entirely for a standalone synth that has no bucket to pass.
        monitor_env = (
            {"OUTBOUND_IVR_MONITOR_BUCKET": monitor_bucket.bucket_name}
            if monitor_bucket is not None
            else {}
        )
        if trace_call_content:
            Annotations.of(self).add_warning(
                "Call content (transcript, prompts, model responses) is being written to "
                "traces and read by the online evaluators. Fine for synthetic data, not "
                "for real patient data: deploy with -c traceCallContent=false first."
            )

        self.runtime = agentcore.Runtime(
            self,
            "OutboundIvrRuntime",
            runtime_name=RUNTIME_NAME,
            description=(
                "Outbound IVR (LiveKit transport) on Nova 2.5 Sonic, dialing the Connect "
                "mock office through the self-hosted LiveKit SIP trunk"
            ),
            agent_runtime_artifact=agent_runtime_artifact,
            execution_role=self.execution_role,
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_vpc(
                self, vpc=vpc, vpc_subnets=private_subnets
            ),
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.minutes(idle_timeout_minutes),
                max_lifetime=Duration.hours(max_lifetime_hours),
            ),
            tracing_enabled=True,
            environment_variables={
                # Plain ws:// on purpose, a documented non-production exception: the
                # host is a private DNS name in this VPC and the Runtime runs in its
                # private subnets, so signaling never leaves the VPC, and call audio is
                # WebRTC (DTLS-SRTP, always encrypted). Production should put TLS in
                # front of LiveKit's signaling port and switch this to wss://.
                "LIVEKIT_URL": f"ws://{livekit_host}:7880",  # nosemgrep: detect-insecure-websocket
                "LIVEKIT_API_KEY": livekit_api_key,
                "LIVEKIT_API_SECRET_ARN": livekit_api_secret_arn,
                "OUTBOUND_IVR_LEVELS": "0",
                # Fifteen seconds of no RTP on a leg and livekit-sip removes the
                # participant, which reaches the agent as `participant_disconnected` --
                # the real hangup signal. Through the Voice Connector a far-end hangup
                # does not reliably produce a BYE the bridge sees, so without this the
                # only thing ending the call is livekit-sip's 120s default, measured on a
                # real call with a human sitting on a dead line for all of it. Safe here
                # and not in the local Asterisk path: Connect and a mobile both send RTP
                # continuously while off-hook. Deliberately well below
                # OUTBOUND_IVR_HANDOFF_SILENCE_TIMEOUT_S so this fires first and the
                # audio-silence backstop stays a backstop. See
                # outbound_ivr.voice.livekit_calls.SIP_MEDIA_TIMEOUT_S.
                "OUTBOUND_IVR_SIP_MEDIA_TIMEOUT_S": "15",
                # Gateway, not local: record_item/escalate/complete/transfer_to_team
                # go through gateway_stack.py's policy-checked Gateway now -- see
                # the design plan's shared contract. Direct DynamoDB access for
                # case/office *reads* (ReadCaseAndOffice/RecordCaseOutcome below)
                # is unaffected either way; the Gateway only gates writes.
                "OUTBOUND_IVR_TOOLS": "gateway",
                "OUTBOUND_IVR_GATEWAY_URL": gateway_url,
                "OUTBOUND_IVR_EVENTS_TABLE": events_table.table_name,
                "OUTBOUND_IVR_CASES_TABLE": cases_table.table_name,
                "OUTBOUND_IVR_OFFICES_TABLE": offices_table.table_name,
                "OUTBOUND_IVR_ARTIFACTS_BUCKET": artifacts_bucket.bucket_name,
                "OUTBOUND_IVR_OFFICE_NUMBER": office_number,
                "OUTBOUND_IVR_TRUNK_NAME": trunk_name,
                "OUTBOUND_IVR_TEAM_DIALER": "aws",
                # Direct mode rings a person's phone (context testCellNumber, from
                # .env FRONT_DESK_NUMBER); connect mode dials the escalation DID.
                "OUTBOUND_IVR_EVIDENCE_NUMBER": (
                    self.node.try_get_context("testCellNumber") or ""
                    if (self.node.try_get_context("evidenceMode") or "connect") == "direct"
                    else evidence_number or ""
                ),
                # "direct" dials OUTBOUND_IVR_EVIDENCE_NUMBER straight into the room (a
                # colleague's cell, no handoff code, no DTMF -- see
                # ../../src/outbound_ivr/voice/backend.py's AwsTeamDialer);
                # "connect" (the default, including when context evidenceMode is
                # unset) keeps the handoff-code-and-DTMF path into Connect's
                # Evidence team queue. See the design plan's shared contract.
                "OUTBOUND_IVR_EVIDENCE_MODE": self.node.try_get_context("evidenceMode")
                or "connect",
                "OUTBOUND_IVR_HANDOFF_TABLE": handoff_table.table_name,
                "OUTBOUND_IVR_SONIC_MODEL": sonic_model,
                # US English, the same voice the local demo uses (.env).
                "OUTBOUND_IVR_VOICE": self.node.try_get_context("sonicVoice") or "tiffany",
                "AWS_REGION": REGION,
                # The call transcript on the call's root span, for Evaluations.
                "OUTBOUND_IVR_TRACE_CALL_CONTENT": "1" if trace_call_content else "0",
                **content_extraction_env,
                **monitor_env,
            },
        )

        # Online evaluation: scores every finished call from the runtime's own
        # spans (a call counts as finished after 15 minutes with no new spans).
        # Results go to the evaluation-results log group and to CloudWatch
        # metrics under Bedrock-AgentCore/Evaluations. Every call is sampled
        # because a demo places only a few; lower this for real traffic.
        self.online_evaluation = agentcore.OnlineEvaluationConfig(
            self,
            "OutboundIvrOnlineEvaluation",
            online_evaluation_config_name=f"{RUNTIME_NAME}_calls",
            description="Scores each of Outbound IVR's calls: goal reached, instructions followed, tool use",
            data_source=agentcore.DataSourceConfig.from_agent_runtime_endpoint(self.runtime),
            evaluators=[
                agentcore.EvaluatorSelector.builtin(evaluator)
                for evaluator in (
                    agentcore.BuiltinEvaluator.GOAL_SUCCESS_RATE,
                    agentcore.BuiltinEvaluator.INSTRUCTION_FOLLOWING,
                    agentcore.BuiltinEvaluator.HELPFULNESS,
                    agentcore.BuiltinEvaluator.TOOL_SELECTION_ACCURACY,
                    agentcore.BuiltinEvaluator.TOOL_PARAMETER_ACCURACY,
                    agentcore.BuiltinEvaluator.HARMFULNESS,
                )
            ],
            sampling_percentage=evaluation_sampling_percentage,
            session_timeout=Duration.minutes(15),
            # Said to default to ENABLED, but left unset the property is omitted
            # and the config is created DISABLED, scoring nothing.
            execution_status=agentcore.ExecutionStatus.ENABLED,
        )
        NagSuppressions.add_resource_suppressions(
            self.online_evaluation,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The evaluation role the OnlineEvaluationConfig construct "
                        "creates: DescribeLogGroups takes no resource, the runtime's "
                        "log streams, aws/spans and the evaluation-results groups "
                        "end in :* or /*, and the built-in evaluators' model is "
                        "chosen by the service and called through cross-region "
                        "inference profiles."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        {"regex": "/^Resource::arn:<AWS::Partition>:bedrock:\\*:.*\\*$/"},
                        {"regex": "/^Resource::arn:<AWS::Partition>:logs:.*\\*$/"},
                    ],
                }
            ],
            apply_to_children=True,
        )

        Tags.of(self).add("project", PROJECT_TAG)
