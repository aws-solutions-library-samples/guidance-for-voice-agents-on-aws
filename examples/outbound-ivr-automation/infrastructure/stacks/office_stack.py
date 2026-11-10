"""OutboundIvrOfficeStack: a standalone, top-level `Stack` -- not a `NestedStack`, and
not a child of `OutboundIvrStack` -- that owns the mock provider office end to end:
the Connect instance, its hours of operation, the front-desk queue, the
security profile, the routing profile, the `outbound-ivr-front-desk` agent user and
its generated password secret, the office IVR contact flow, and the office
DID plus its flow association. Deployed on its own, before and independently
of `OutboundIvrStack`.

**Why a separate top-level stack, not a nested child of `OutboundIvrStack`.** The
mock office is not part of Outbound IVR; it is the third party Outbound IVR CALLS -- a real
customer would point this app at an actual provider's phone system instead of
this one. Folding it into `OutboundIvrStack` also created a genuine CloudFormation
dependency cycle: `FrontDeskNestedStack` claimed the office DID and built the
office flow, but `AgentRuntimeNestedStack` (which needs that number to dial)
had to be built first, and `DashboardNestedStack` sat between them --
`AgentRuntime -> FrontDesk -> Dashboard -> AgentRuntime`. The workaround was a
two-pass deploy: deploy once, read the claimed number
off a CfnOutput by hand, put it in `.env`, deploy again. This stack removes
the cycle by removing the shared CloudFormation stack it depended on: this
office exists, and its number is known, before Outbound IVR is ever synthesized.

**It is long-lived; `OutboundIvrStack` is not.** You claim a DID once and keep it --
tearing `OutboundIvrStack` down and rebuilding it (this app has already replaced
its own nested-stack layout wholesale more than once) must not release the
office's phone number. Nothing in this file carries `RemovalPolicy.RETAIN`,
but nothing here is meant to be torn down on the same cadence as `OutboundIvrStack`
either; that is a property of how this stack is operated, not of its code.

**The SSM contract -- what replaces the stack dependency.** This stack writes
four parameters, every deploy, unconditionally:

| Path | Value |
|---|---|
| `/outbound-ivr/connect/instance-id` | the Connect instance id |
| `/outbound-ivr/connect/instance-arn` | the Connect instance arn |
| `/outbound-ivr/connect/office-number` | the claimed office DID in E.164, or the literal `+15555550101` placeholder when `claimOfficeNumber` is false |
| `/outbound-ivr/connect/front-desk-queue-arn` | the front-desk queue arn |

All four, always -- including `office-number` when no DID is claimed. A
parameter that sometimes does not exist would make a reader's own lookup fail
at deploy, which is exactly the fragility this contract exists to remove.
`OutboundIvrStack` is meant to read each of these with
`ssm.StringParameter.from_string_parameter_name(self, "<id>", "<path>").string_value`
-- a CloudFormation dynamic reference resolved at *deploy* time, the same
pattern `livekit_stack.py` already uses for the nine `/outbound-ivr/voice/*`
parameters it reads from the live `OutboundIvrVoiceStack` stack -- never
`value_from_lookup`, which resolves at *synth* time and needs AWS credentials.
Reading an SSM parameter *name* this way is not a CloudFormation stack
dependency, so no cycle is possible no matter what either stack's own
internal resource graph looks like.

**`claimOfficeNumber` defaults to TRUE here.** Every other context flag of
this shape in this app (`claimEvidenceNumber` in escalation_stack.py, and this
same flag in the frontdesk_stack.py this absorbs it from) defaults to False,
because claiming a number there was optional set dressing on top of a stack
whose main job was something else. Here claiming a number *is* the job: this
stack exists to BE a dialable office, so leaving it undialable by default
would be a strange default. Pass `-c claimOfficeNumber=false` to opt out
(synthesizes the office flow with no number to call it on, same as the old
default). Claiming a DID costs about a dollar a month.

**`CONNECT_MANAGED`, inbound-only.** Outbound IVR dials in to this instance from
LiveKit for the mock office call -- see the Outbound IVR Runtime's own module
docstring -- and nothing here ever asks Connect to place a call, so
`outbound_calls=False`.

**The alias carries the account id.** A Connect instance alias must be
globally unique across every AWS account, not just this one -- it becomes the
CCP domain, `<alias>.my.connect.aws` -- so a plain `outbound-ivr-office` could
collide with a wholly different account. `outbound_ivr_stack.py`'s dashboard derives
the same CCP URL from this exact `f"{RESOURCE_PREFIX}-office-{ACCOUNT_ID}"`
string directly, from constants, without depending on this stack at all.

**What stayed behind in `frontdesk_stack.py`.** The CCP approved-origin
association -- letting the dashboard's embedded CCP call this instance from
its own CloudFront origin -- is the one piece of "office" work that is
actually about Outbound IVR's own dashboard, not the office itself, so it is not
absorbed here. It needs this stack's instance *id*, not its arn
(`AssociateApprovedOrigin`'s own `InstanceId` parameter documents the
instance's identifier, not its ARN -- see frontdesk_stack.py's own module
docstring), read out of `/outbound-ivr/connect/instance-id` (the contract above)
rather than off a cross-stack construct reference.

**The office flow's one placeholder is checked, not trusted.**
`${FRONT_DESK_QUEUE_ARN}` (`connect/make_flow.py`'s own constant) is the only
placeholder the committed "queue" variant of the office flow
(`connect/flows/outbound-ivr-mock-provider-ivr.json`) carries. Synth fails loudly if
that placeholder is ever missing, instead of deploying a flow with a literal,
unresolvable `${...}` baked into its `Content`. CloudFormation treats a contact
flow's `Content` as an opaque string, and Amazon Connect only validates it on
upload, so neither `cdk synth` nor cdk-nag catches a bad substitution here --
only this explicit check does.

Still synth only. Same ground rules as the rest of this app: no `cdk deploy`.
"""

from __future__ import annotations

import json
import os

from aws_cdk import (
    Annotations,
    CfnOutput,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import (
    aws_connect as connect,
)
from aws_cdk import (
    aws_iam as iam,
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

from .constants import ACCOUNT_ID, PROJECT_TAG, RESOURCE_PREFIX

# The SSM contract (module docstring). These four /outbound-ivr/connect/* paths are
# this stack's own; they collide with nothing OutboundIvrVoiceStack's
# /outbound-ivr/voice/* paths own.
INSTANCE_ID_PARAMETER = f"/{RESOURCE_PREFIX}/connect/instance-id"
INSTANCE_ARN_PARAMETER = f"/{RESOURCE_PREFIX}/connect/instance-arn"
OFFICE_NUMBER_PARAMETER = f"/{RESOURCE_PREFIX}/connect/office-number"
FRONT_DESK_QUEUE_ARN_PARAMETER = f"/{RESOURCE_PREFIX}/connect/front-desk-queue-arn"

# Published to /outbound-ivr/connect/office-number (and the OfficePhoneNumberOutput
# CfnOutput) whenever no DID is claimed -- the same obviously-fake E.164
# placeholder livekit_stack.py's own `officeNumber` context default and
# agent_runtime_stack.py's own fallback already use, so a reader of either
# this parameter or that context value sees the same "nothing claimed yet"
# value either way.
OFFICE_NUMBER_PLACEHOLDER = "+15555550101"

# connect/make_flow.py's own FRONT_DESK_QUEUE_ARN constant: the one
# placeholder the committed "queue" variant of the office flow (connect/flows/
# outbound-ivr-mock-provider-ivr.json) carries (module docstring).
OFFICE_FLOW_QUEUE_ARN_PLACEHOLDER = "${FRONT_DESK_QUEUE_ARN}"


def _bool_context(node, key: str, *, default: bool) -> bool:
    """Parse a CDK context flag as a boolean, defaulting to `default` when unset.

    `-c key=true` / `-c key=false` on the CDK CLI always arrives as the
    *string* "true"/"false" (context values set programmatically, e.g. a
    test's `cdk.App(context=...)`, may already be real bools) -- so the naive
    `bool(value or default)` this app's other context flags use treats the
    string "false" as truthy and can never actually turn a *True* default
    off. That is harmless for a flag that defaults False (an operator who
    wants it off just omits it), but `claimOfficeNumber` defaults True here
    (module docstring) specifically so an operator CAN turn it off, so this
    stack needs a parser that actually honors "false".
    """
    value = node.try_get_context(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    raise ValueError(f"context {key}={value!r} is not a valid boolean (true/false)")


class OutboundIvrOfficeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        agent_email = self.node.try_get_context("agentEmail")
        if not agent_email:
            raise ValueError(
                "Missing required context value 'agentEmail' (the front desk "
                "agent user's login identity -- the password is generated into "
                "Secrets Manager, see this stack's module docstring). Pass "
                "-c agentEmail=you@example.com; never commit a real email to "
                "cdk.context.json."
            )
        agent_first_name = self.node.try_get_context("agentFirstName") or "Front"
        agent_last_name = self.node.try_get_context("agentLastName") or "Desk"
        # Resolved relative to this file, not the working directory, so it
        # doesn't depend on where synth is run from -- same convention as
        # escalation_stack.py's own escalationLookupLambdaDir default. One
        # parent up from infrastructure/stacks/ reaches infrastructure/, where
        # connect/ lives.
        contact_flow_path = self.node.try_get_context("contactFlowPath") or os.path.join(
            os.path.dirname(__file__),
            "..",
            "connect",
            "flows",
            "outbound-ivr-mock-provider-ivr.json",
        )
        # Default TRUE, not False -- see this stack's own module docstring for
        # why, and _bool_context's docstring for why a plain `bool(x or True)`
        # could not actually implement that default.
        claim_office_number = _bool_context(self.node, "claimOfficeNumber", default=True)

        # ------------------------------------------------------------------
        # The one Connect instance this stack owns outright. Globally unique
        # alias across accounts, not just this one -- module docstring's "The
        # alias carries the account id". "-office", not "-escalation" or
        # "-front-desk": the alias/CCP concept is singular per instance.
        # ------------------------------------------------------------------
        self.instance_alias: str = f"{RESOURCE_PREFIX}-office-{ACCOUNT_ID}"
        instance = connect.CfnInstance(
            self,
            "ConnectInstance",
            identity_management_type="CONNECT_MANAGED",
            instance_alias=self.instance_alias,
            attributes=connect.CfnInstance.AttributesProperty(
                inbound_calls=True,
                outbound_calls=False,
                contactflow_logs=True,
            ),
        )
        self.instance_arn: str = instance.attr_arn
        self.instance_id: str = instance.attr_id

        # ------------------------------------------------------------------
        # Hours of operation, queue -- 24/7: this is a demo queue, not a real
        # rota, same convention as escalation_stack.py's own AlwaysOpenHours.
        # ------------------------------------------------------------------
        always_open = connect.CfnHoursOfOperation(
            self,
            "FrontDeskHours",
            name=f"{RESOURCE_PREFIX}-front-desk-always-open",
            instance_arn=self.instance_arn,
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

        self.queue = connect.CfnQueue(
            self,
            "FrontDeskQueue",
            # Connect's default agent whisper reads the queue name to whoever answers,
            # so it is a phrase a person would say, not an outbound-ivr-* identifier.
            name="Prior authorization desk",
            instance_arn=self.instance_arn,
            hours_of_operation_arn=always_open.attr_hours_of_operation_arn,
            description="outbound_ivr: front desk queue, the mock office's prior-auth transfer target",
        )

        # ------------------------------------------------------------------
        # Office IVR contact flow: the mock provider office's menu (connect/
        # make_flow.py, regenerated from connect/prompts.py's shared wording),
        # transferring 3-3-2 callers into the queue just created above. Read
        # from the committed file, not built in-process
        # here -- see connect/README.md for how it's regenerated.
        #
        # ${FRONT_DESK_QUEUE_ARN} is the one placeholder the committed
        # "queue" variant of that file carries (connect/make_flow.py's own
        # FRONT_DESK_QUEUE_ARN constant). Checking it is actually present
        # before substituting -- rather than trusting the substitution --
        # fails synth loudly on a flow that would otherwise deploy with a
        # literal, unresolvable "${FRONT_DESK_QUEUE_ARN}" baked in.
        # ------------------------------------------------------------------
        with open(contact_flow_path, encoding="utf-8") as handle:
            office_flow_content = handle.read()
        if OFFICE_FLOW_QUEUE_ARN_PLACEHOLDER not in office_flow_content:
            raise ValueError(
                f"Office contact flow {contact_flow_path!r} does not contain "
                f"the placeholder {OFFICE_FLOW_QUEUE_ARN_PLACEHOLDER!r} "
                "(connect/make_flow.py's own FRONT_DESK_QUEUE_ARN constant), "
                "so nothing would wire it to the front desk queue just "
                "created above. Fix the flow file, or pass "
                "-c contactFlowPath=... to one that carries it."
            )
        office_flow_content = office_flow_content.replace(
            OFFICE_FLOW_QUEUE_ARN_PLACEHOLDER, self.queue.attr_queue_arn
        )
        # Well-formed JSON before handing it to CloudFormation: a bad
        # substitution or an in-progress flow regeneration should fail synth
        # loudly, not deploy a broken flow (CloudFormation treats Content as
        # an opaque string -- module docstring).
        json.loads(office_flow_content)

        self.office_flow = connect.CfnContactFlow(
            self,
            "OfficeContactFlow",
            name=f"{RESOURCE_PREFIX}-office-mock-provider-ivr",
            instance_arn=self.instance_arn,
            type="CONTACT_FLOW",
            description=(
                "outbound_ivr: AnyCompany Medical Group's mock IVR, transferring "
                "3-3-2 callers to the front desk queue"
            ),
            content=office_flow_content,
        )
        self.office_flow_arn = self.office_flow.attr_contact_flow_arn

        # ------------------------------------------------------------------
        # Claimed office DID: gated behind a context flag that defaults TRUE
        # here (module docstring), unlike every other claim flag in this app.
        # Copies escalation_stack.py's own claimed-DID block's CfnPhoneNumber +
        # AwsCustomResource association pattern exactly: AWS::Connect::PhoneNumber
        # only claims a number to the instance, AssociatePhoneNumberContactFlow
        # (no native CloudFormation resource for it) is the real Connect API
        # that points it at a flow.
        # ------------------------------------------------------------------
        self.office_phone_number: str | None = None
        if claim_office_number:
            office_did = connect.CfnPhoneNumber(
                self,
                "OfficeDid",
                target_arn=self.instance_arn,
                country_code="US",
                type="DID",
                description="outbound_ivr: mock provider office DID",
            )
            office_did_association = cr.AwsCustomResource(
                self,
                "OfficeDidFlowAssociation",
                resource_type="Custom::ConnectPhoneNumberContactFlowAssociation",
                install_latest_aws_sdk=True,
                on_create=cr.AwsSdkCall(
                    service="Connect",
                    action="associatePhoneNumberContactFlow",
                    parameters={
                        "PhoneNumberId": office_did.attr_phone_number_arn,
                        "InstanceId": self.instance_arn,
                        "ContactFlowId": self.office_flow.attr_contact_flow_arn,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"{RESOURCE_PREFIX}-office-did-flow-association"
                    ),
                ),
                on_update=cr.AwsSdkCall(
                    service="Connect",
                    action="associatePhoneNumberContactFlow",
                    parameters={
                        "PhoneNumberId": office_did.attr_phone_number_arn,
                        "InstanceId": self.instance_arn,
                        "ContactFlowId": self.office_flow.attr_contact_flow_arn,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"{RESOURCE_PREFIX}-office-did-flow-association"
                    ),
                ),
                # Unlink before CloudFormation deletes the flow: Connect
                # refuses to delete a flow a phone number still points at.
                on_delete=cr.AwsSdkCall(
                    service="Connect",
                    action="disassociatePhoneNumberContactFlow",
                    parameters={
                        "PhoneNumberId": office_did.attr_phone_number_arn,
                        "InstanceId": self.instance_arn,
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
                                office_did.attr_phone_number_arn,
                                self.office_flow.attr_contact_flow_arn,
                            ],
                        )
                    ]
                ),
            )
            office_did_association.node.add_dependency(office_did)
            office_did_association.node.add_dependency(self.office_flow)
            NagSuppressions.add_resource_suppressions(
                office_did_association,
                [
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": (
                            "AwsCustomResource's generated Lambda uses the "
                            "AWS-authored AWSLambdaBasicExecutionRole for "
                            "CloudWatch Logs only; standard CDK "
                            "custom-resource pattern, same as "
                            "escalation_stack.py's equivalent association."
                        ),
                    },
                ],
                apply_to_children=True,
            )
            self.office_phone_number = office_did.attr_address
            # Same placement as escalation_stack.py's own claimed-DID block:
            # this covers the AwsCustomResource framework's shared singleton
            # provider Lambda, which exists only when a custom resource like
            # the one above is actually created, so it belongs inside this
            # `if`, not unconditionally at stack level.
            NagSuppressions.add_stack_suppressions(
                self,
                [
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": (
                            "Covers the AwsCustomResource framework's shared "
                            "singleton provider Lambda, which AWS CDK attaches "
                            "AWSLambdaBasicExecutionRole to for CloudWatch Logs "
                            "only; no customer-authored equivalent. Same "
                            "pattern as escalation_stack.py's own claimed-DID "
                            "block."
                        ),
                        "appliesTo": [
                            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                        ],
                    }
                ],
            )
        else:
            Annotations.of(self).add_warning(
                "claimOfficeNumber context is false: synthesizing the office "
                "contact flow only, without claiming a DID for it. "
                f"{OFFICE_NUMBER_PARAMETER} and the OfficePhoneNumberOutput "
                f"CfnOutput below carry the {OFFICE_NUMBER_PLACEHOLDER!r} "
                "placeholder instead of a real number until you claim one -- "
                "omit claimOfficeNumber (true is the default) or pass "
                "-c claimOfficeNumber=true to claim a real DID, for about a "
                "dollar a month."
            )

        agent_security_profile = connect.CfnSecurityProfile(
            self,
            "FrontDeskAgentSecurityProfile",
            security_profile_name=f"{RESOURCE_PREFIX}-front-desk-agent",
            instance_arn=self.instance_arn,
            description="Minimal front desk agent profile: CCP access only",
            # "BasicAgentAccess" (UI name "Access Contact Control Panel") is the
            # one permission a CCP-only agent needs -- confirmed against
            # https://docs.aws.amazon.com/connect/latest/adminguide/security-profile-list.html,
            # same as escalation_stack.py's own agent security profile.
            permissions=["BasicAgentAccess"],
        )

        routing_profile = connect.CfnRoutingProfile(
            self,
            "FrontDeskRoutingProfile",
            name=f"{RESOURCE_PREFIX}-front-desk-routing",
            description="outbound_ivr: routes voice contacts from the front desk queue",
            instance_arn=self.instance_arn,
            default_outbound_queue_arn=self.queue.attr_queue_arn,
            media_concurrencies=[
                connect.CfnRoutingProfile.MediaConcurrencyProperty(channel="VOICE", concurrency=1)
            ],
            queue_configs=[
                connect.CfnRoutingProfile.RoutingProfileQueueConfigProperty(
                    queue_reference=connect.CfnRoutingProfile.RoutingProfileQueueReferenceProperty(
                        queue_arn=self.queue.attr_queue_arn, channel="VOICE"
                    ),
                    priority=1,
                    delay=0,
                )
            ],
        )

        # Connect requires a password for a user on a CONNECT_MANAGED instance.
        # It is generated into Secrets Manager and reaches CloudFormation only as
        # a dynamic reference, never as a template literal -- same convention as
        # escalation_stack.py's own AgentPassword. Read it with
        # `aws secretsmanager get-secret-value --secret-id outbound_ivr/front-desk-agent-password`.
        agent_password = secretsmanager.Secret(
            self,
            "FrontDeskAgentPassword",
            secret_name=f"{RESOURCE_PREFIX}/front-desk-agent-password",
            description="outbound_ivr: login password for the front desk agent user (outbound-ivr-front-desk)",
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
                        "A Connect user's login password, set once on create; "
                        "Connect has no rotation integration for it. Demo user "
                        "on a shared mock-office instance."
                    ),
                }
            ],
        )
        CfnOutput(self, "FrontDeskAgentPasswordSecretName", value=agent_password.secret_name)

        self.agent_user = connect.CfnUser(
            self,
            "FrontDeskAgentUser",
            username=f"{RESOURCE_PREFIX}-front-desk",
            instance_arn=self.instance_arn,
            routing_profile_arn=routing_profile.attr_routing_profile_arn,
            security_profile_arns=[agent_security_profile.attr_security_profile_arn],
            password=agent_password.secret_value.unsafe_unwrap(),
            phone_config=connect.CfnUser.UserPhoneConfigProperty(
                phone_type="SOFT_PHONE", auto_accept=False
            ),
            identity_info=connect.CfnUser.UserIdentityInfoProperty(
                email=agent_email,
                first_name=agent_first_name,
                last_name=agent_last_name,
            ),
        )

        # ------------------------------------------------------------------
        # The SSM contract (module docstring): all four written unconditionally,
        # every deploy, so a reader (OutboundIvrStack, scripts/deploy_flow.sh,
        # connect/make_flow.py) never has to special-case a parameter that might
        # not exist yet.
        # ------------------------------------------------------------------
        self.office_number: str = self.office_phone_number or OFFICE_NUMBER_PLACEHOLDER

        ssm.StringParameter(
            self,
            "InstanceIdParameter",
            parameter_name=INSTANCE_ID_PARAMETER,
            string_value=self.instance_id,
            description="outbound_ivr: this app's own Connect instance id, for scripts/deploy_flow.sh",
        )
        ssm.StringParameter(
            self,
            "InstanceArnParameter",
            parameter_name=INSTANCE_ARN_PARAMETER,
            string_value=self.instance_arn,
            description=(
                "outbound_ivr: this app's own Connect instance arn, read by OutboundIvrStack "
                "with no CloudFormation stack dependency (see this stack's own "
                "module docstring's SSM contract)"
            ),
        )
        ssm.StringParameter(
            self,
            "OfficeNumberParameter",
            parameter_name=OFFICE_NUMBER_PARAMETER,
            string_value=self.office_number,
            description=(
                "outbound_ivr: the claimed office DID in E.164, or the "
                f"{OFFICE_NUMBER_PLACEHOLDER!r} placeholder when claimOfficeNumber "
                "is false -- always present, never a conditional parameter"
            ),
        )
        ssm.StringParameter(
            self,
            "FrontDeskQueueArnParameter",
            parameter_name=FRONT_DESK_QUEUE_ARN_PARAMETER,
            string_value=self.queue.attr_queue_arn,
            description="outbound_ivr: the front desk queue's ARN, for connect/make_flow.py's queue transfer target",
        )

        Tags.of(self).add("project", PROJECT_TAG)

        CfnOutput(self, "ConnectInstanceId", value=self.instance_id)
        CfnOutput(self, "ConnectInstanceAlias", value=self.instance_alias)
        CfnOutput(self, "OfficePhoneNumberOutput", value=self.office_number)
        CfnOutput(self, "FrontDeskQueueArn", value=self.queue.attr_queue_arn)
        CfnOutput(self, "OfficeContactFlowArn", value=self.office_flow_arn)
