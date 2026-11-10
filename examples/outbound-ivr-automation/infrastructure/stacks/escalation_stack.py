"""outbound-ivr-escalation: a DID, queue, routing profile, and agent user for the
warm-transfer escalation path -- see docs/escalation-handoff.md.

The scenario this answers: Outbound IVR (dialing out through LiveKit, see livekit_stack.py)
reaches a dead end on the mock office call and needs to hand off to a human. Outbound IVR
reads a 6-digit handoff code over DTMF to a second number (this stack's claimed DID),
which rings into Connect, looks the code up, whispers a summary to whichever agent
picks up, and connects them -- while, on the LiveKit side (not built here: nothing in
LiveKit is an AWS resource; see docs/escalation-handoff.md), the office leg is held and
Outbound IVR's own participant leaves once the agent and the office are talking directly.

**Shares the app's own Connect instance; does not create one.** `office_stack.py`'s
`OutboundIvrOfficeStack` is a separate top-level stack that creates the ONE Connect
instance this app owns, along with the mock office's front desk, IVR flow and DID. The
evidence team below attaches to that same instance, whose arn `outbound_ivr_stack.py`
reads from the SSM parameter the office stack publishes, and passes in as this stack's
`instance_arn` constructor argument. A second, dedicated instance here would only be a
second Connect instance to pay for and administer, for nothing gained.

A `NestedStack` (one of `outbound_ivr_stack.py`'s children), not a top-level
`cdk.Stack` -- see outbound_ivr_stack.py's module docstring. `self.handoff_table` and
`self.evidence_phone_number` are read by `agent_runtime_stack.py` to set
`OUTBOUND_IVR_HANDOFF_TABLE`/`OUTBOUND_IVR_EVIDENCE_NUMBER` on the Outbound IVR Runtime at deploy
time (both are CloudFormation-resolved values -- a nested stack's own attribute, not a
cross-top-level-stack SSM read, so this works with no SSM endpoint at all).

Several of the Connect flow-language action types below could not be confirmed against
AWS's own JSON reference (CloudFormation treats `Content` as an opaque string, so
nothing here -- not `cdk synth`, not cdk-nag, not this stack's own tests -- actually
validates them; only a real Connect import would). See the "Known-unverified flow
actions" comment below.
"""

from __future__ import annotations

import json
import os
import uuid

from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    NestedStack,
    RemovalPolicy,
    Tags,
)
from aws_cdk import (
    aws_connect as connect,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as _lambda,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    custom_resources as cr,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from .constants import PROJECT_TAG, RESOURCE_PREFIX

# Fixed, arbitrary namespace for the three flows' action identifiers below, same
# determinism convention as ../../connect/make_flow.py (uuid5 of this
# namespace plus a short name -> stable across runs, readable in a diff).
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, f"{RESOURCE_PREFIX}-escalation")

# ----------------------------------------------------------------------------------
# Every action Type, Parameters key and error type below is checked against the
# Amazon Connect flow-language reference (docs.aws.amazon.com/connect/latest/devguide/
# flow-language-actions.html). A first deploy failed on guessed names (LoopPrompts,
# StoreUserInput, SetCustomerQueueFlow); the real ones, and where each is allowed:
#   - "Store customer input" is GetParticipantInput with StoreInput "True",
#     InputValidation.CustomValidation.MaximumLength and
#     DTMFConfiguration.InputTerminationSequence; the digits land in
#     $.StoredCustomerInput.
#   - "Set customer queue flow" is UpdateContactEventHooks, EventHooks.CustomerQueue.
#   - "Loop prompts" is MessageParticipantIteratively (Customer Queue flows); it ends
#     through the "MessagesInterrupted" condition after InterruptFrequencySeconds.
#   - EndFlowExecution is allowed only in whisper and customer queue flows; a
#     successful TransferContactToQueue in an inbound flow just ends it.
# Connect's own validation then rejected three more details (CreateContactFlow
# returns a "problems" list that CloudFormation hides): UpdateFlowLoggingBehavior
# takes no error branches, store-input GetParticipantInput takes only
# NoMatchingError, and TransferContactToQueue needs a NextAction naming a real action
# even though a successful transfer never takes it.
# ----------------------------------------------------------------------------------


def _aid(name: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, name))


def _action(name: str, action_type: str, parameters: dict, transitions: dict) -> dict:
    return {
        "Identifier": _aid(name),
        "Type": action_type,
        "Parameters": parameters,
        "Transitions": transitions,
    }


def _simple_transition(next_name: str) -> dict:
    return {
        "NextAction": _aid(next_name),
        "Errors": [{"NextAction": _aid(next_name), "ErrorType": "NoMatchingError"}],
        "Conditions": [],
    }


def _build_handoff_flow_content(
    *, lambda_arn: str, queue_arn: str, queue_hold_flow_arn: str
) -> str:
    """The Evidence team's inbound flow: read a 6-digit handoff code, look it up,
    refuse the call if the code is not recognized, otherwise set contact attributes
    from the result, point the hold experience at the Customer Queue flow below, then
    transfer to the Evidence team queue."""
    actions = [
        _action(
            "voice",
            "UpdateContactTextToSpeechVoice",
            {"TextToSpeechVoice": "Joanna", "TextToSpeechEngine": "neural"},
            _simple_transition("logging"),
        ),
        _action(
            "logging",
            "UpdateFlowLoggingBehavior",
            {"FlowLoggingBehavior": "Enabled"},
            # No error branches: Connect rejects any on this action.
            {"NextAction": _aid("store-code"), "Errors": [], "Conditions": []},
        ),
        _action(
            "store-code",
            "GetParticipantInput",
            {
                "Text": "Please enter the six digit handoff code you were given, then press pound.",
                "InputTimeLimitSeconds": "15",
                "StoreInput": "True",
                "InputValidation": {"CustomValidation": {"MaximumLength": "6"}},
                "DTMFConfiguration": {"InputTerminationSequence": "#"},
            },
            {
                "NextAction": _aid("save-code-attr"),
                # With StoreInput "True" the only error branch Connect accepts is
                # NoMatchingError; a timeout lands there too.
                "Errors": [
                    {"NextAction": _aid("code-timeout-message"), "ErrorType": "NoMatchingError"}
                ],
                "Conditions": [],
            },
        ),
        _action(
            "code-timeout-message",
            "MessageParticipant",
            {"Text": "We did not receive a code. Goodbye."},
            _simple_transition("disconnect"),
        ),
        _action(
            "save-code-attr",
            "UpdateContactAttributes",
            {"Attributes": {"handoff_code": "$.StoredCustomerInput"}, "TargetContact": "Current"},
            _simple_transition("invoke-lookup"),
        ),
        _action(
            "invoke-lookup",
            "InvokeLambdaFunction",
            {
                "LambdaFunctionARN": lambda_arn,
                "InvocationTimeLimitSeconds": "8",
                "ResponseValidation": {"ResponseType": "STRING_MAP"},
            },
            {
                "NextAction": _aid("check-code-found"),
                "Errors": [
                    {"NextAction": _aid("lookup-error-message"), "ErrorType": "NoMatchingError"}
                ],
                "Conditions": [],
            },
        ),
        _action(
            # The gate. Without this, the lookup's own answer is ignored: the Lambda
            # returns found "false" for an unrecognized or missing code (see
            # ../lambdas/escalation_lookup/handler.py), and the flow used to transfer
            # to a human anyway, just with empty case attributes. Since this stack
            # claims a public DID, that made the handoff code decorative -- any six
            # digits reached the Evidence team.
            #
            # Compare takes a single JSONPath in ComparisonValue and raises
            # NoMatchingCondition when no Condition matches (flow-control-actions-
            # compare.html); Equals is an exact string match on one operand
            # (flow-language-actions.html, "List of Operators"). found is the literal
            # string "true"/"false", not a boolean, because the Lambda's response
            # validation is STRING_MAP.
            "check-code-found",
            "Compare",
            {"ComparisonValue": "$.External.found"},
            {
                # Required and must name a real action; reached when no Condition
                # matches, same as the NoMatchingCondition branch below.
                "NextAction": _aid("code-not-recognized-message"),
                "Errors": [
                    {
                        "NextAction": _aid("code-not-recognized-message"),
                        "ErrorType": "NoMatchingCondition",
                    }
                ],
                "Conditions": [
                    {
                        "NextAction": _aid("save-case-attrs"),
                        "Condition": {"Operator": "Equals", "Operands": ["true"]},
                    }
                ],
            },
        ),
        _action(
            "code-not-recognized-message",
            "MessageParticipant",
            {"Text": "That handoff code was not recognized. Goodbye."},
            # Deliberately a dead end, not a transfer. An unrecognized code is the one
            # case where failing closed matters: the caller never proved they are the
            # agent this handoff was prepared for.
            _simple_transition("disconnect"),
        ),
        _action(
            "lookup-error-message",
            "MessageParticipant",
            {
                "Text": "We could not look up that code right now. Please hold while we transfer you."
            },
            # Fail open, and only here: this branch is an infrastructure failure (the
            # Lambda did not answer at all), not a wrong code. A real agent holding a
            # valid code should not be hung up on because DynamoDB had a bad minute.
            _simple_transition("set-customer-queue-flow"),
        ),
        _action(
            "save-case-attrs",
            "UpdateContactAttributes",
            {
                "Attributes": {
                    "case_id": "$.External.case_id",
                    "items_collected": "$.External.items_collected",
                    "escalation_reason": "$.External.escalation_reason",
                    "summary": "$.External.summary",
                },
                "TargetContact": "Current",
            },
            _simple_transition("set-customer-queue-flow"),
        ),
        _action(
            "set-customer-queue-flow",
            "UpdateContactEventHooks",
            {"EventHooks": {"CustomerQueue": queue_hold_flow_arn}},
            _simple_transition("set-queue"),
        ),
        _action(
            "set-queue",
            "UpdateContactTargetQueue",
            {"QueueId": queue_arn},
            _simple_transition("transfer-queue"),
        ),
        _action(
            "transfer-queue",
            "TransferContactToQueue",
            {},
            {
                # Required, and must be a real action, but never taken on success:
                # the contact is in the queue and this flow is over.
                "NextAction": _aid("queue-full-message"),
                "Errors": [
                    {"NextAction": _aid("queue-full-message"), "ErrorType": "QueueAtCapacity"},
                    {"NextAction": _aid("queue-full-message"), "ErrorType": "NoMatchingError"},
                ],
                "Conditions": [],
            },
        ),
        _action(
            "queue-full-message",
            "MessageParticipant",
            {"Text": "We're sorry, the evidence team queue is full right now. Goodbye."},
            _simple_transition("disconnect"),
        ),
        _action("disconnect", "DisconnectParticipant", {}, {}),
    ]
    return json.dumps({"Version": "2019-10-30", "StartAction": _aid("voice"), "Actions": actions})


def _build_queue_hold_flow_content() -> str:
    """Customer Queue flow: hold music/announcement loop, then a callback message
    on timeout. No real automated callback is placed -- out of scope for this
    example; see docs/escalation-handoff.md."""
    actions = [
        _action(
            "loop-prompts",
            "MessageParticipantIteratively",
            {
                "Messages": [
                    {
                        "Text": "Please continue to hold for the evidence team. We appreciate your patience."
                    }
                ],
                # About three minutes on hold before the callback message.
                "InterruptFrequencySeconds": "180",
            },
            {
                "NextAction": _aid("callback-message"),
                "Errors": [
                    {"NextAction": _aid("callback-message"), "ErrorType": "NoMatchingError"}
                ],
                "Conditions": [
                    {
                        "NextAction": _aid("callback-message"),
                        "Condition": {"Operator": "Equals", "Operands": ["MessagesInterrupted"]},
                    }
                ],
            },
        ),
        _action(
            "callback-message",
            "MessageParticipant",
            {
                "Text": "We're sorry, no one is available right now. We will call you back as soon as possible. Goodbye."
            },
            _simple_transition("disconnect"),
        ),
        _action("disconnect", "DisconnectParticipant", {}, {}),
    ]
    return json.dumps(
        {"Version": "2019-10-30", "StartAction": _aid("loop-prompts"), "Actions": actions}
    )


def _build_whisper_flow_content() -> str:
    """Agent whisper flow: read the one-line summary (and case id / reason) to
    whichever agent the queue connects, with Polly, before the customer joins."""
    actions = [
        _action(
            "voice",
            "UpdateContactTextToSpeechVoice",
            {"TextToSpeechVoice": "Joanna", "TextToSpeechEngine": "neural"},
            _simple_transition("whisper-message"),
        ),
        _action(
            "whisper-message",
            "MessageParticipant",
            {
                "Text": "Case $.Attributes.case_id. Reason for escalation: $.Attributes.escalation_reason. Summary: $.Attributes.summary."
            },
            _simple_transition("end-flow"),
        ),
        _action("end-flow", "EndFlowExecution", {}, {}),
    ]
    return json.dumps({"Version": "2019-10-30", "StartAction": _aid("voice"), "Actions": actions})


class EscalationNestedStack(NestedStack):
    def __init__(self, scope: Construct, construct_id: str, *, instance_arn: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        agent_email = self.node.try_get_context("agentEmail")
        if not agent_email:
            raise ValueError(
                "Missing required context value 'agentEmail' (the user's own "
                "email, for the Evidence team agent user's login identity -- "
                "the password is generated into Secrets Manager). Pass "
                "-c agentEmail=you@example.com; never commit a real "
                "email to cdk.context.json."
            )
        agent_first_name = self.node.try_get_context("agentFirstName") or "Evidence"
        agent_last_name = self.node.try_get_context("agentLastName") or "Agent"
        claim_evidence_number = bool(self.node.try_get_context("claimEvidenceNumber") or False)
        lookup_lambda_dir = self.node.try_get_context("escalationLookupLambdaDir") or os.path.join(
            os.path.dirname(__file__), "..", "..", "src", "lambdas", "escalation_lookup"
        )

        # ------------------------------------------------------------------
        # DynamoDB: one item per handoff code, written by AwsTeamDialer right
        # before the evidence-team leg is dialed (see
        # outbound_ivr.voice.backend.AwsTeamDialer). TTL'd and DESTROY on stack
        # deletion: synthetic, short-lived test data, same reasoning as
        # data_construct.py's events table.
        # ------------------------------------------------------------------
        self.handoff_table = dynamodb.Table(
            self,
            "HandoffCodesTable",
            table_name=f"{RESOURCE_PREFIX}-handoff-codes",
            partition_key=dynamodb.Attribute(name="code", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Lookup Lambda: reads the handoff_code contact attribute, returns a flat
        # STRING_MAP (case_id, items_collected, escalation_reason, summary) --
        # see lambdas/escalation_lookup/handler.py.
        # ------------------------------------------------------------------
        self.lookup_lambda = _lambda.Function(
            self,
            "HandoffLookupFunction",
            function_name=f"{RESOURCE_PREFIX}-escalation-lookup",
            runtime=_lambda.Runtime.PYTHON_3_14,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(lookup_lambda_dir),
            timeout=Duration.seconds(8),
            # An explicit group, not `log_retention`: the implicit group the Lambda
            # service creates never expires, and `log_retention` fixes that by
            # standing up a singleton custom-resource Lambda whose role holds
            # logs:PutRetentionPolicy on "*". Declaring the group directly costs
            # nothing and keeps every IAM statement in this stack scoped. Same
            # pattern as the tools Lambda in gateway_stack.py. These logs are the
            # only record of why a handoff lookup failed. The function's
            # LoggingConfig points at whatever group it is given, so the name
            # does not have to match the function name, and this group is
            # destroyed along with the rest of this stack.
            log_group=logs.LogGroup(
                self,
                "HandoffLookupLogGroup",
                log_group_name=f"/{RESOURCE_PREFIX}/lambda/escalation-lookup",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={"HANDOFF_TABLE": self.handoff_table.table_name},
        )
        # Read plus UpdateItem, not full read/write: the lookup claims a code with
        # one conditional UpdateItem (single-use, see the handler) and never puts or
        # deletes items.
        self.handoff_table.grant_read_data(self.lookup_lambda)
        self.handoff_table.grant(self.lookup_lambda, "dynamodb:UpdateItem")
        self.lookup_lambda.add_permission(
            "AllowConnectInvoke",
            principal=iam.ServicePrincipal("connect.amazonaws.com"),
            source_arn=instance_arn,
        )
        NagSuppressions.add_resource_suppressions(
            self.lookup_lambda,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "Default Function execution role's basic-execution "
                        "policy (CloudWatch Logs only), the standard CDK "
                        "default for a Lambda with no custom role passed in; "
                        "no customer-authored equivalent."
                    ),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                }
            ],
            apply_to_children=True,
        )

        # A real, native CloudFormation resource (unlike the Chime/Connect
        # custom resources elsewhere in this app): associates the Lambda with
        # the instance so it is selectable from a flow's "Invoke AWS Lambda
        # function" block at all.
        integration = connect.CfnIntegrationAssociation(
            self,
            "HandoffLambdaIntegration",
            instance_id=instance_arn,
            integration_type="LAMBDA_FUNCTION",
            integration_arn=self.lookup_lambda.function_arn,
        )
        integration.node.add_dependency(self.lookup_lambda)

        # ------------------------------------------------------------------
        # Hours of operation, queue, routing profile, security profile, agent
        # user. 24/7: this is a demo queue, not a real rota.
        # ------------------------------------------------------------------
        always_open = connect.CfnHoursOfOperation(
            self,
            "AlwaysOpenHours",
            name=f"{RESOURCE_PREFIX}-always-open",
            instance_arn=instance_arn,
            time_zone="America/New_York",
            config=[
                connect.CfnHoursOfOperation.HoursOfOperationConfigProperty(
                    day=day,
                    start_time=connect.CfnHoursOfOperation.HoursOfOperationTimeSliceProperty(
                        hours=0, minutes=0
                    ),
                    end_time=connect.CfnHoursOfOperation.HoursOfOperationTimeSliceProperty(
                        hours=23, minutes=59
                    ),
                )
                for day in (
                    "MONDAY",
                    "TUESDAY",
                    "WEDNESDAY",
                    "THURSDAY",
                    "FRIDAY",
                    "SATURDAY",
                    "SUNDAY",
                )
            ],
        )

        self.evidence_queue = connect.CfnQueue(
            self,
            "EvidenceTeamQueue",
            name="Evidence team",
            instance_arn=instance_arn,
            hours_of_operation_arn=always_open.attr_hours_of_operation_arn,
            description="outbound_ivr: escalation handoff queue",
        )

        agent_security_profile = connect.CfnSecurityProfile(
            self,
            "AgentSecurityProfile",
            security_profile_name=f"{RESOURCE_PREFIX}-agent",
            instance_arn=instance_arn,
            description="Minimal agent profile: CCP access only",
            # "BasicAgentAccess" (UI name "Access Contact Control Panel") is the
            # one permission a CCP-only agent needs -- confirmed against
            # https://docs.aws.amazon.com/connect/latest/adminguide/security-profile-list.html,
            # not a guess like the flow actions above.
            permissions=["BasicAgentAccess"],
        )

        routing_profile = connect.CfnRoutingProfile(
            self,
            "EvidenceRoutingProfile",
            name=f"{RESOURCE_PREFIX}-evidence-routing",
            description="outbound_ivr: routes voice contacts from the Evidence team queue",
            instance_arn=instance_arn,
            default_outbound_queue_arn=self.evidence_queue.attr_queue_arn,
            media_concurrencies=[
                connect.CfnRoutingProfile.MediaConcurrencyProperty(channel="VOICE", concurrency=1)
            ],
            queue_configs=[
                connect.CfnRoutingProfile.RoutingProfileQueueConfigProperty(
                    queue_reference=connect.CfnRoutingProfile.RoutingProfileQueueReferenceProperty(
                        queue_arn=self.evidence_queue.attr_queue_arn, channel="VOICE"
                    ),
                    priority=1,
                    delay=0,
                )
            ],
        )

        # Connect requires a password for a user on a CONNECT_MANAGED instance.
        # It is generated into Secrets Manager and reaches CloudFormation only as
        # a dynamic reference, never as a template literal. Read it with
        # `aws secretsmanager get-secret-value --secret-id <AgentPasswordSecretName>`.
        agent_password = secretsmanager.Secret(
            self,
            "AgentPassword",
            secret_name=f"{RESOURCE_PREFIX}/evidence-agent-password",
            description=f"outbound_ivr: login password for the Evidence team agent user ({RESOURCE_PREFIX}-agent)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=20,
                exclude_punctuation=True,
                require_each_included_type=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        NagSuppressions.add_resource_suppressions(
            agent_password,
            [
                {
                    "id": "AwsSolutions-SMG4",
                    "reason": (
                        "A Connect user's login password, set once on create; Connect has "
                        "no rotation integration for it."
                    ),
                }
            ],
        )
        CfnOutput(self, "AgentPasswordSecretName", value=agent_password.secret_name)
        self.agent_user = connect.CfnUser(
            self,
            "EvidenceAgentUser",
            username=f"{RESOURCE_PREFIX}-agent",
            instance_arn=instance_arn,
            routing_profile_arn=routing_profile.attr_routing_profile_arn,
            security_profile_arns=[agent_security_profile.attr_security_profile_arn],
            password=agent_password.secret_value.unsafe_unwrap(),
            phone_config=connect.CfnUser.UserPhoneConfigProperty(phone_type="SOFT_PHONE"),
            identity_info=connect.CfnUser.UserIdentityInfoProperty(
                email=agent_email,
                first_name=agent_first_name,
                last_name=agent_last_name,
            ),
        )

        # ------------------------------------------------------------------
        # The three flows. Content built in-process above --
        # see this module's "Known-unverified flow actions" comment.
        # ------------------------------------------------------------------
        self.queue_hold_flow = connect.CfnContactFlow(
            self,
            "EvidenceQueueHoldFlow",
            name=f"{RESOURCE_PREFIX}-evidence-queue-hold",
            instance_arn=instance_arn,
            type="CUSTOMER_QUEUE",
            description="outbound_ivr: hold loop + callback-on-timeout message for the Evidence team queue",
            content=_build_queue_hold_flow_content(),
        )

        self.whisper_flow = connect.CfnContactFlow(
            self,
            "EvidenceWhisperFlow",
            name=f"{RESOURCE_PREFIX}-evidence-whisper",
            instance_arn=instance_arn,
            type="AGENT_WHISPER",
            description="outbound_ivr: reads the case id, escalation reason, and summary to the agent",
            content=_build_whisper_flow_content(),
        )

        self.handoff_flow = connect.CfnContactFlow(
            self,
            "EvidenceHandoffFlow",
            name=f"{RESOURCE_PREFIX}-evidence-handoff",
            instance_arn=instance_arn,
            type="CONTACT_FLOW",
            description="outbound_ivr: reads the 6-digit handoff code, looks it up, transfers to the Evidence team queue",
            content=_build_handoff_flow_content(
                lambda_arn=self.lookup_lambda.function_arn,
                queue_arn=self.evidence_queue.attr_queue_arn,
                queue_hold_flow_arn=self.queue_hold_flow.attr_contact_flow_arn,
            ),
        )
        self.handoff_flow.node.add_dependency(integration)

        # ------------------------------------------------------------------
        # Claimed DID: gated behind a context flag, same convention and
        # reasoning as office_stack.py's claimOfficeNumber -- costs money per
        # day, so off by default.
        # ------------------------------------------------------------------
        self.evidence_phone_number: str | None = None
        if claim_evidence_number:
            phone_number = connect.CfnPhoneNumber(
                self,
                "EvidenceDid",
                target_arn=instance_arn,
                country_code="US",
                type="DID",
                description="outbound_ivr: evidence team handoff DID",
            )
            association = cr.AwsCustomResource(
                self,
                "EvidenceDidFlowAssociation",
                resource_type="Custom::ConnectPhoneNumberContactFlowAssociation",
                install_latest_aws_sdk=True,
                on_create=cr.AwsSdkCall(
                    service="Connect",
                    action="associatePhoneNumberContactFlow",
                    parameters={
                        "PhoneNumberId": phone_number.attr_phone_number_arn,
                        "InstanceId": instance_arn,
                        "ContactFlowId": self.handoff_flow.attr_contact_flow_arn,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"{RESOURCE_PREFIX}-evidence-did-flow-association"
                    ),
                ),
                on_update=cr.AwsSdkCall(
                    service="Connect",
                    action="associatePhoneNumberContactFlow",
                    parameters={
                        "PhoneNumberId": phone_number.attr_phone_number_arn,
                        "InstanceId": instance_arn,
                        "ContactFlowId": self.handoff_flow.attr_contact_flow_arn,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"{RESOURCE_PREFIX}-evidence-did-flow-association"
                    ),
                ),
                # Unlink before CloudFormation deletes the flow: Connect refuses to
                # delete a flow a phone number still points at.
                on_delete=cr.AwsSdkCall(
                    service="Connect",
                    action="disassociatePhoneNumberContactFlow",
                    parameters={
                        "PhoneNumberId": phone_number.attr_phone_number_arn,
                        "InstanceId": instance_arn,
                    },
                    ignore_error_codes_matching="ResourceNotFoundException",
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements(
                    [
                        iam.PolicyStatement(
                            actions=[
                                "connect:AssociatePhoneNumberContactFlow",
                                "connect:DisassociatePhoneNumberContactFlow",
                            ],
                            # Scoped to this number and this flow: Associate is
                            # authorized against both, Disassociate against the
                            # number (Service Authorization Reference, Connect).
                            resources=[
                                phone_number.attr_phone_number_arn,
                                self.handoff_flow.attr_contact_flow_arn,
                            ],
                        )
                    ]
                ),
            )
            association.node.add_dependency(phone_number)
            association.node.add_dependency(self.handoff_flow)
            NagSuppressions.add_resource_suppressions(
                association,
                [
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": (
                            "AwsCustomResource's generated Lambda uses the "
                            "AWS-authored AWSLambdaBasicExecutionRole for "
                            "CloudWatch Logs only; standard CDK "
                            "custom-resource pattern."
                        ),
                    },
                ],
                apply_to_children=True,
            )
            self.evidence_phone_number = phone_number.attr_address
            CfnOutput(self, "EvidenceDidOutput", value=phone_number.attr_address)

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
        else:
            Annotations.of(self).add_warning(
                "claimEvidenceNumber context is not set: synthesizing the "
                "Evidence team's queue, flows, and agent user only, without "
                "claiming a DID (that costs money per day). Pass "
                "-c claimEvidenceNumber=true to also claim one."
            )

        CfnOutput(self, "EscalationInstanceArn", value=instance_arn)
        CfnOutput(self, "EvidenceQueueArn", value=self.evidence_queue.attr_queue_arn)
        CfnOutput(self, "HandoffTableName", value=self.handoff_table.table_name)

        Tags.of(self).add("project", PROJECT_TAG)
