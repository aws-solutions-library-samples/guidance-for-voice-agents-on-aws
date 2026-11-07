"""OutboundIvrStack: one top-level `Stack` with ten `NestedStack` children -- network,
LiveKit, escalation, data, Monitor, the tools Gateway, the Outbound IVR Runtime,
FrontDesk, the dashboard, and ops (the alarms).

The mock provider office lives entirely outside this stack, in
`OutboundIvrOfficeStack` (`office_stack.py`), a second, independent top-level `Stack`
this app synthesizes alongside this one (see `app.py`) -- the Connect instance, its
hours of operation, the front desk's queue/routing profile/security profile/agent
user, the office IVR contact flow, and the claimed office DID all live there. Nothing
below builds a Connect instance. FrontDesk, still built here (`frontdesk_stack.py`),
keeps only the one piece of the mock office that is genuinely Outbound IVR's own
concern: the dashboard's CCP approved-origin grant (see that file's own module
docstring for why that one resource can't move with the rest).

**The mock office is not Outbound IVR; it is the third party Outbound IVR calls.** A real
customer would point this app at an actual provider's phone system instead of
this one. Folding it into this stack also produced a genuine CloudFormation
dependency cycle: FrontDesk claimed the office DID, but AgentRuntime (which
needs that number to dial) had to be built before FrontDesk, and FrontDesk
itself had to be built after Dashboard (for the dashboard's own CloudFront
domain, FrontDesk's approved-origin target) -- `AgentRuntime -> FrontDesk ->
Dashboard -> AgentRuntime`. The workaround was a two-pass deploy: deploy once,
read the claimed number off a CfnOutput by hand, pass it back in as
`-c officeNumber=...`, deploy again.

**The SSM contract -- what actually kills the two-pass deploy.**
`OutboundIvrOfficeStack` writes four parameters unconditionally, every deploy (its
own module docstring):

| Path | Value |
|---|---|
| `/outbound-ivr/connect/instance-id` | the Connect instance id |
| `/outbound-ivr/connect/instance-arn` | the Connect instance arn |
| `/outbound-ivr/connect/office-number` | the claimed office DID in E.164, or the `+15555550101` placeholder |
| `/outbound-ivr/connect/front-desk-queue-arn` | the front-desk queue arn |

This stack reads the first three below with
`ssm.StringParameter.from_string_parameter_name(self, "<id>", "<path>").string_value`
-- a CloudFormation dynamic reference CloudFormation itself resolves at
*deploy* time, never a synth-time context/credentials lookup
(`value_from_lookup`) -- the same pattern `livekit_stack.py` already uses for
`OutboundIvrVoiceStack`'s nine `/outbound-ivr/voice/*` parameters, below. (The fourth,
front-desk-queue-arn, has no reader in this stack -- see the comment on it
below.) Reading a parameter
*name* this way is not a CloudFormation stack dependency: `cdk synth
OutboundIvrStack` never requires `OutboundIvrOfficeStack` to already be deployed, and
nothing below ever imports or constructs `OutboundIvrOfficeStack` -- the two stacks
share a parameter-name convention, not a Python object. Because the read
carries no synth-time ordering requirement either, `agent_runtime` gets the
real office number -- the claimed DID, or `OutboundIvrOfficeStack`'s own documented
placeholder -- on this stack's very first build, not a second deploy after a
manual copy-paste. These four `/outbound-ivr/connect/*` paths are their own
namespace, distinct from the seven `/outbound-ivr/voice/*` paths
`OutboundIvrVoiceStack` (`voice_stack.py`) publishes -- do not collide the two.

`OutboundIvrVoiceStack` (`stacks/voice_stack.py`) stays its own separate stack, never
nested here -- it deploys once, ahead of this stack, and this app only ever reads its
SSM parameter *names*, which is nesting-agnostic. Renaming its logical ids would
replace the EIPs and Voice Connector and break the LiveKit host's outbound trunk
registration, so its construct ids stay fixed once deployed.

Cross-nested-stack wiring that is NOT the SSM contract above -- LiveKit's fixed
private IP/API key/secret ARN into the Runtime; the Runtime's own ARN into the
dashboard Lambda's IAM/env; the escalation stack's handoff table/evidence DID
into the Runtime -- is just passing Python objects/CDK tokens between
constructors; CDK wires the underlying nested-stack parameters/outputs
automatically, no `Fn::ImportValue` needed. That mechanism, and the SSM
contract above, are both only for separate top-level stacks -- exactly the
boundary `OutboundIvrOfficeStack` now sits across, and exactly why it gets SSM
instead of a constructor argument.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from .agent_runtime_stack import AgentRuntimeNestedStack
from .constants import ACCOUNT_ID, PROJECT_TAG, RESOURCE_PREFIX
from .dashboard_stack import DashboardNestedStack
from .data_stack import DataNestedStack
from .escalation_stack import EscalationNestedStack
from .frontdesk_stack import FrontDeskNestedStack
from .gateway_stack import GatewayNestedStack
from .livekit_stack import LiveKitNestedStack
from .monitor_stack import MonitorNestedStack
from .network_stack import NetworkNestedStack
from .ops_stack import OpsNestedStack


class OutboundIvrStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        network = NetworkNestedStack(self, "Network")

        livekit = LiveKitNestedStack(
            self,
            "LiveKit",
            vpc=network.vpc,
            public_subnets=network.public_subnets,
            private_subnets=network.private_subnets,
        )
        livekit.add_dependency(network)

        # ------------------------------------------------------------------
        # The mock office: read from SSM, never built here -- see this
        # module's own docstring for the contract `OutboundIvrOfficeStack`
        # (`office_stack.py`, a separate top-level stack deployed on its own,
        # before and independently of this one) writes unconditionally,
        # every deploy. Each read is a CloudFormation dynamic reference,
        # resolved at *deploy* time, so `cdk synth OutboundIvrStack` never requires
        # OutboundIvrOfficeStack to already exist, and nothing in this file ever
        # constructs or imports it.
        # ------------------------------------------------------------------
        office_instance_id = ssm.StringParameter.from_string_parameter_name(
            self, "OfficeInstanceIdParam", "/outbound-ivr/connect/instance-id"
        ).string_value
        office_instance_arn = ssm.StringParameter.from_string_parameter_name(
            self, "OfficeInstanceArnParam", "/outbound-ivr/connect/instance-arn"
        ).string_value
        office_number = ssm.StringParameter.from_string_parameter_name(
            self, "OfficeNumberParam", "/outbound-ivr/connect/office-number"
        ).string_value
        # The fourth contract parameter, /outbound-ivr/connect/front-desk-queue-arn, is
        # deliberately not read here: nothing in this stack needs the queue's
        # own arn (connect/make_flow.py and scripts/deploy_flow.sh, both
        # outside this CDK app, are its real readers -- the same split
        # frontdesk_stack.py's own, now-deleted FRONT_DESK_QUEUE_ARN_PARAMETER
        # used before the office moved out). An
        # AWS::SSM::Parameter::Value<String> this stack never references
        # would just be dead template weight -- CloudFormation-Validate flags
        # exactly that as an unused parameter -- so it is left to the readers
        # that actually use it.
        escalation = EscalationNestedStack(self, "Escalation", instance_arn=office_instance_arn)

        data = DataNestedStack(self, "Data")

        # Monitor: one bucket (see monitor_stack.py), after Data (shares its
        # KMS key) and before AgentRuntime/Dashboard (both write/read it).
        monitor = MonitorNestedStack(self, "Monitor", data_kms_key=data.kms_key)
        monitor.add_dependency(data)

        # Gateway before the Runtime: the Runtime's env needs the Gateway's own
        # MCP URL (OUTBOUND_IVR_GATEWAY_URL below), and its execution role needs
        # bedrock-agentcore:InvokeGateway on the Gateway's ARN -- granted once
        # the Runtime (and its execution role) exist, below. See
        # gateway_stack.py's module docstring and the design plan's shared
        # contract.
        gateway = GatewayNestedStack(
            self,
            "Gateway",
            cases_table=data.cases_table,
            offices_table=data.offices_table,
        )
        gateway.add_dependency(data)

        agent_runtime = AgentRuntimeNestedStack(
            self,
            "AgentRuntime",
            vpc=network.vpc,
            private_subnets=network.private_subnet_selection,
            events_table=data.events_table,
            cases_table=data.cases_table,
            offices_table=data.offices_table,
            artifacts_bucket=data.artifacts_bucket,
            handoff_table=escalation.handoff_table,
            evidence_number=escalation.evidence_phone_number,
            livekit_host=livekit.livekit_host,
            livekit_api_key=livekit.livekit_api_key,
            livekit_api_secret_arn=livekit.livekit_keys_secret.secret_arn,
            gateway_url=gateway.gateway_url,
            # The real office number -- OutboundIvrOfficeStack's claimed DID, or its
            # documented placeholder -- read from SSM above, before AgentRuntime
            # (or anything else in this stack) is even synthesized. See
            # agent_runtime_stack.py's own comment on this parameter for why
            # that is no longer a CloudFormation cycle.
            office_phone_number=office_number,
            monitor_bucket=monitor.bucket,
        )
        agent_runtime.add_dependency(network)
        agent_runtime.add_dependency(livekit)
        agent_runtime.add_dependency(escalation)
        agent_runtime.add_dependency(data)
        agent_runtime.add_dependency(gateway)
        agent_runtime.add_dependency(monitor)

        # Cross-nested-stack IAM grant, done here at the composition root (not
        # inside either nested stack) -- same convention this file already
        # uses for every other cross-stack grant (see the module docstring).
        # Granted after agent_runtime exists, since the grant targets its
        # execution role.
        gateway.gateway.grant_invoke(agent_runtime.execution_role)

        # Front desk's CCP URL -- derived from the SAME deterministic alias
        # office_stack.py's OutboundIvrOfficeStack gives the instance
        # (f"{RESOURCE_PREFIX}-office-{ACCOUNT_ID}", the alias that forms the
        # CCP domain <alias>.my.connect.aws), built here from constants
        # rather than read off OutboundIvrOfficeStack (which has no "CCP url"
        # concept of its own, just the instance alias -- see its own module
        # docstring) or off a fifth SSM parameter this string does not need.
        # Dashboard must never depend on FrontDesk (FrontDesk instead depends
        # on Dashboard's own CloudFront domain, for its approved-origin
        # association; a dependency the other way around would be circular)
        # -- deriving the string from constants keeps Dashboard free of any
        # Connect-related dependency at all, nested or cross-stack.
        ccp_url = f"https://{RESOURCE_PREFIX}-office-{ACCOUNT_ID}.my.connect.aws/ccp-v2/"

        dashboard = DashboardNestedStack(
            self,
            "Dashboard",
            cases_table=data.cases_table,
            offices_table=data.offices_table,
            events_table=data.events_table,
            artifacts_bucket=data.artifacts_bucket,
            data_kms_key=data.kms_key,
            agent_runtime_arn=agent_runtime.runtime.agent_runtime_arn,
            monitor_bucket=monitor.bucket,
            ccp_url=ccp_url,
        )
        dashboard.add_dependency(data)
        dashboard.add_dependency(agent_runtime)
        dashboard.add_dependency(livekit)
        dashboard.add_dependency(monitor)

        # No cross-nested-stack IAM grant needed here: the API Lambda's
        # monitor-bucket IAM (ListBucket/GetObject/PutObject, scoped to the
        # bucket and its seg/watch prefixes) is granted directly inside
        # dashboard_construct.py's `DashboardConstruct`, the same nested
        # stack as the role itself, so there is no
        # role-in-one-nested-stack/resource-in-another split to bridge at
        # this composition root.

        # Front desk: now just the CCP approved-origin grant (see
        # frontdesk_stack.py's own module docstring for why everything else
        # that used to live here -- the queue, hours, routing/security
        # profiles, the agent user, the office flow, the claimed DID --
        # moved to OutboundIvrOfficeStack). Takes the Connect instance id read from
        # SSM above (AssociateApprovedOrigin's own InstanceId parameter, not
        # an arn) and Dashboard's CloudFront domain -- still built after
        # Dashboard, for that domain; see the ccp_url comment above for why
        # that dependency runs only this one direction.
        front_desk = FrontDeskNestedStack(
            self,
            "FrontDesk",
            instance_id=office_instance_id,
            dashboard_origin=f"https://{dashboard.distribution.distribution_domain_name}",
        )
        front_desk.add_dependency(dashboard)

        # Ops last: it only reads names and metrics off the others, and an alarm
        # is worth nothing if the thing it watches failed to create.
        ops = OpsNestedStack(
            self,
            "Ops",
            kms_key=data.kms_key,
            cluster_name=livekit.cluster_name,
            service_name=livekit.service_name,
            host_group_name=livekit.host_group_name,
            tables={"cases": data.cases_table, "events": data.events_table},
            functions={
                "tools": gateway.tools_lambda,
                "lookup": escalation.lookup_lambda,
                "api": dashboard.api_lambda,
            },
        )
        ops.add_dependency(livekit)
        ops.add_dependency(data)
        ops.add_dependency(gateway)
        ops.add_dependency(escalation)
        ops.add_dependency(dashboard)

        self.network = network
        self.livekit = livekit
        self.escalation = escalation
        self.data = data
        self.monitor = monitor
        self.gateway = gateway
        self.agent_runtime = agent_runtime
        self.dashboard = dashboard
        self.front_desk = front_desk
        self.ops = ops
        self.office_instance_id = office_instance_id
        self.office_instance_arn = office_instance_arn
        self.office_number = office_number

        # Re-declared at the top level because nested-stack CfnOutputs (see
        # DashboardNestedStack) never surface in `cdk deploy`'s own Outputs
        # printout or in `aws cloudformation describe-stacks
        # --stack-name OutboundIvrStack` -- only this stack's own outputs do.
        # These are the two values the "Try it" / "Deploy" steps in README.md
        # need (the dashboard's sign-in URL and the Cognito pool to create a
        # user in); keep README.md's instructions pointed at these names.
        CfnOutput(
            self,
            "DashboardUrl",
            value=f"https://{dashboard.distribution.distribution_domain_name}",
        )
        CfnOutput(self, "DashboardUserPoolId", value=dashboard.users_pool.user_pool_id)

        Tags.of(self).add("project", PROJECT_TAG)
