"""Assertions for outbound-ivr-network: public + private subnets exist, no NAT
gateway (point (a)/(e) need no outbound-internet path for the VPC-internal traffic
this app generates -- see stacks/network_stack.py's docstring), and a Secrets
Manager interface endpoint is always present (so the LiveKit host's boot-time
secret fetches do not depend on its Elastic IP having attached yet)."""

from __future__ import annotations

import json

from stacks.constants import REGION


def test_no_nat_gateways(network_template) -> None:
    network_template.resource_count_is("AWS::EC2::NatGateway", 0)


def test_secrets_manager_endpoint_always_present(network_template) -> None:
    # No add_logs_endpoint=True needed for this one, unlike the CloudWatch Logs
    # endpoint: the LiveKit host's user-data always fetches three secrets at
    # boot (see stacks/livekit_stack.py), so it is on by default.
    endpoints = network_template.find_resources("AWS::EC2::VPCEndpoint")
    # The S3 gateway endpoint's ServiceName is an Fn::Join, not a plain string.
    services = {
        e["Properties"]["ServiceName"]
        for e in endpoints.values()
        if isinstance(e["Properties"]["ServiceName"], str)
    }
    assert f"com.amazonaws.{REGION}.secretsmanager" in services, (
        f"expected a Secrets Manager interface endpoint, found services: {services}"
    )


def test_has_public_and_private_subnets(network_template) -> None:
    subnets = network_template.find_resources("AWS::EC2::Subnet")
    public = [
        s
        for s in subnets.values()
        if any(t.get("Value") == "outbound-ivr" for t in s["Properties"].get("Tags", []))
    ]
    assert len(public) >= 4, "expected at least 2 public + 2 private subnets, all tagged"


def test_vpc_is_tagged(network_template) -> None:
    vpcs = network_template.find_resources("AWS::EC2::VPC")
    (vpc,) = vpcs.values()
    tags = {t["Key"]: t["Value"] for t in vpc["Properties"].get("Tags", [])}
    assert tags.get("project") == "outbound-ivr"


def test_isolated_subnets_can_pull_container_images(network_template) -> None:
    """AgentCore in VPC mode pulls its image from ECR (layers from S3) through
    these subnets, which have no internet route."""
    services = json.dumps(network_template.find_resources("AWS::EC2::VPCEndpoint"))
    for name in ("ecr.api", "ecr.dkr", ".s3"):
        assert name in services, f"missing the {name} endpoint"


def test_gateway_and_xray_interface_endpoints_present(network_template) -> None:
    """The Runtime's own agent code now calls the Gateway's MCP endpoint and
    (via ADOT) the X-Ray trace-export endpoint directly -- neither is covered
    by the generic bedrock-agentcore data-plane endpoint or the existing logs
    endpoint. See network_stack.py's module docstring."""
    endpoints = network_template.find_resources("AWS::EC2::VPCEndpoint")
    services = {
        e["Properties"]["ServiceName"]
        for e in endpoints.values()
        if isinstance(e["Properties"]["ServiceName"], str)
    }
    assert f"com.amazonaws.{REGION}.bedrock-agentcore.gateway" in services
    assert f"com.amazonaws.{REGION}.xray" in services
