"""Fixed facts shared by every outbound-ivr-* stack in this app."""

from __future__ import annotations

import os

# The deploy target. Read from the environment so a reader deploying into their own
# account does not have to edit source: CDK_DEFAULT_ACCOUNT/CDK_DEFAULT_REGION are set
# by the CDK CLI from whatever credentials and profile are in use;
# OUTBOUND_IVR_ACCOUNT_ID/OUTBOUND_IVR_REGION override them explicitly (for a synth
# with no credentials at all, for instance). There is no fallback default for either:
# a placeholder account/region would make a misconfigured deploy silently target the
# wrong place instead of failing loudly. Tests that synth without real credentials set
# OUTBOUND_IVR_ACCOUNT_ID/OUTBOUND_IVR_REGION themselves (see tests/infrastructure/conftest.py).
ACCOUNT_ID = os.environ.get("OUTBOUND_IVR_ACCOUNT_ID") or os.environ.get("CDK_DEFAULT_ACCOUNT")
if not ACCOUNT_ID:
    raise RuntimeError(
        "No AWS account configured: set OUTBOUND_IVR_ACCOUNT_ID, or run cdk through a CLI "
        "environment (AWS profile/credentials) that populates CDK_DEFAULT_ACCOUNT."
    )

# amazon.nova-2-5-sonic and the Chime SDK Voice Connector are only available in a
# handful of Regions; see the README's Prerequisites/Cost sections for which ones.
# No fallback default here either -- deploying to an unintended Region is worse than
# failing fast.
REGION = os.environ.get("OUTBOUND_IVR_REGION") or os.environ.get("CDK_DEFAULT_REGION")
if not REGION:
    raise RuntimeError(
        "No AWS Region configured: set OUTBOUND_IVR_REGION, or run cdk through a CLI "
        "environment (AWS profile/credentials/region) that populates CDK_DEFAULT_REGION."
    )

# Shared literal prefix for every AWS-visible resource name this app creates
# (bucket/table/queue/etc. names, not CDK construct ids).
RESOURCE_PREFIX = "outbound-ivr"

PROJECT_TAG = "outbound-ivr"

# Default outbound trunk name the agent Runtime dials through -- see
# agent_runtime_stack.py and the shared contract in the design plan. Same
# value livekit_stack.py's own trunk-setup container registers.
DEFAULT_TRUNK_NAME = f"{RESOURCE_PREFIX}-chime-outbound"

DEFAULT_SONIC_MODEL = "amazon.nova-2-5-sonic"

# Chime SDK Voice Connector signaling + media CIDR for the default Region above, per
# https://docs.aws.amazon.com/chime-sdk/latest/ag/network-config.html (confirmed
# 2026-10-16: SIP signaling and SRTP media use the *same* CIDR in this Region, unlike
# some other Regions where they differ. Override if deploying to a different Region.
CHIME_VOICE_CONNECTOR_CIDR = "3.80.16.0/23"

# SIP TLS signaling to/from Chime.
SIP_TLS_PORT = 5061

# Chime SDK Voice Connector SRTP media range: a subset of Chime's documented
# UDP/5000:65000, large enough for a handful of simultaneous test calls.
SIP_RTP_PORT_RANGE = (10000, 20000)

# LiveKit server: HTTP/WebSocket signaling (client SDK + server API) and the TCP
# fallback for WebRTC when UDP is blocked. Both are VPC-CIDR-only in this app: no
# public dashboard, no softphone -- only the AgentCore Runtime test (e) and an
# operator's SSM port-forward need them, and both reach the VPC either directly (the
# Runtime) or through SSM (never a new inbound rule).
LIVEKIT_SIGNALING_PORT = 7880
LIVEKIT_TCP_FALLBACK_PORT = 7881

# LiveKit's WebRTC/ICE UDP media range. Production guides size this in the
# thousands of ports; an app with one or two test calls needs far fewer. Keep it
# small and explicit so the security group stays readable, not because fewer ports
# are "safer" against the VPC CIDR (which is already a closed, trusted range).
WEBRTC_UDP_PORT_RANGE = (50000, 50019)

# ElastiCache Valkey's default port (same for Redis OSS; LiveKit does not care which
# engine answers on it).
VALKEY_PORT = 6379
