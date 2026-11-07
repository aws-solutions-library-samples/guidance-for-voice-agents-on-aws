#!/usr/bin/env python3
"""CDK entry point for Outbound IVR Automation's AWS deployment.

    cd infrastructure
    uv sync --extra infra --extra dev
    uv run cdk synth \\
      -c testCellNumber=+15555550199 -c agentEmail=you@example.com

Three top-level stacks, all synthesized by this one app:

- `OutboundIvrVoiceStack` (voice_stack.py) -- the permanent Chime SDK Voice Connector
  and its Elastic IP pool, shared across redeploys of the stack below. Deployed once
  per account and never torn down (`RemovalPolicy.RETAIN` on the connector and both
  EIPs); deploy it first.
- `OutboundIvrOfficeStack` (office_stack.py) -- the mock provider office: its own
  Connect instance, hours of operation, front-desk queue, routing profile, agent user,
  IVR flow, and claimed DID. Long-lived -- deploy it once, ahead of `OutboundIvrStack`,
  and leave it running; tearing `OutboundIvrStack` down and rebuilding it must never
  touch this stack or release its claimed number.
- `OutboundIvrStack` (outbound_ivr_stack.py), with nested children (see that file) --
  the agent itself: network, LiveKit, escalation (the evidence team), data, the tools
  Gateway, the agent Runtime, FrontDesk (the dashboard's CCP approved-origin grant),
  the dashboard, and ops. Reads what it needs about the office off
  `OutboundIvrOfficeStack`'s own SSM parameters, never as a construct reference.
"""

from __future__ import annotations

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks.constants import ACCOUNT_ID, REGION
from stacks.office_stack import OutboundIvrOfficeStack
from stacks.outbound_ivr_stack import OutboundIvrStack
from stacks.voice_stack import VoiceStack

app = cdk.App()
env = cdk.Environment(account=ACCOUNT_ID, region=REGION)

voice = VoiceStack(
    app,
    "OutboundIvrVoiceStack",
    env=env,
    description=("outbound-ivr: the permanent Chime SDK Voice Connector and its Elastic IP pool"),
)

office = OutboundIvrOfficeStack(
    app,
    "OutboundIvrOfficeStack",
    env=env,
    description=(
        "outbound-ivr-office: the mock provider office -- its own Connect instance, "
        "hours of operation, front-desk queue, routing profile, the "
        "outbound-ivr-front-desk agent user, the office IVR flow, and its claimed "
        "DID. Deployed independently of OutboundIvrStack, and long-lived: tearing "
        "OutboundIvrStack down must never release the claimed number. Publishes its "
        "instance id/arn, office number, and front-desk queue arn to SSM for "
        "OutboundIvrStack to read -- see this stack's own module docstring"
    ),
)

outbound_ivr = OutboundIvrStack(
    app,
    "OutboundIvrStack",
    env=env,
    description=(
        "outbound-ivr: the agent on LiveKit + AgentCore -- network, LiveKit on ECS, "
        "escalation (Connect), data, the tools Gateway, the agent Runtime, "
        "FrontDesk's CCP approved-origin grant, the dashboard, and ops. Reads the "
        "mock office's Connect instance and claimed number from "
        "OutboundIvrOfficeStack's own SSM parameters, never as a construct reference"
    ),
)
# Ordering only -- which stack `cdk deploy --all` (or `destroy --all`) touches first,
# recorded in the cloud assembly manifest; not a CloudFormation cross-stack reference.
# OutboundIvrStack's own SSM reads (outbound_ivr_stack.py's module docstring) are what
# actually let it synthesize and deploy with no dependency on OutboundIvrOfficeStack's
# internal resource graph.
outbound_ivr.add_dependency(office)

cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
