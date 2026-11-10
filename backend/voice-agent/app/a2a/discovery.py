"""CloudMap service discovery for A2A capability agents.

Discovers A2A-compatible agents registered in an AWS CloudMap HTTP namespace.
Each agent is an ECS Fargate service that auto-registers on startup and
auto-deregisters on shutdown.

Usage:
    endpoints = await discover_agents("voice-agent-capabilities")
    # Returns: [AgentEndpoint(name="kb-agent", url="http://10.0.1.5:8080"), ...]
"""

import os
from dataclasses import dataclass
from typing import List, Optional

import structlog
from aiobotocore.session import AioSession, get_session

logger = structlog.get_logger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


@dataclass
class AgentEndpoint:
    """A discovered A2A agent endpoint.

    Attributes:
        name: Service name in CloudMap (e.g., "kb-agent")
        url: HTTP URL to reach the agent (e.g., "http://10.0.1.5:8080")
        instance_id: CloudMap instance ID
    """

    name: str
    url: str
    instance_id: Optional[str] = None


async def discover_agents(
    namespace: str,
    region: Optional[str] = None,
    session: Optional[AioSession] = None,
) -> List[AgentEndpoint]:
    """Discover all healthy A2A agents in a CloudMap namespace.

    Queries CloudMap for all services in the namespace, then discovers
    healthy instances for each service. Returns endpoint URLs constructed
    from instance IP and port attributes.

    Args:
        namespace: CloudMap HTTP namespace name (e.g., "voice-agent-capabilities")
        region: AWS region. Defaults to AWS_REGION env var.
        session: Optional aiobotocore session for testing/injection.

    Returns:
        List of AgentEndpoint for each healthy agent instance.
        Empty list if no agents found or on error.
    """
    region = region or AWS_REGION
    _session = session or get_session()

    endpoints: List[AgentEndpoint] = []

    try:
        async with _session.create_client(
            "servicediscovery", region_name=region
        ) as client:
            # First, find the namespace ID
            namespace_id = await _find_namespace_id(client, namespace)
            if not namespace_id:
                logger.warning(
                    "cloudmap_namespace_not_found",
                    namespace=namespace,
                )
                return []

            # List all services in the namespace
            service_names = await _list_services(client, namespace_id)
            if not service_names:
                logger.info(
                    "cloudmap_no_services",
                    namespace=namespace,
                )
                return []

            # Discover healthy instances for each service
            for service_name in service_names:
                try:
                    agent_endpoints = await _discover_service_instances(
                        client, namespace, service_name
                    )
                    endpoints.extend(agent_endpoints)
                except Exception as e:
                    logger.warning(
                        "cloudmap_service_discovery_failed",
                        service=service_name,
                        error=str(e),
                    )
                    continue

    except Exception as e:
        logger.error(
            "cloudmap_discovery_failed",
            namespace=namespace,
            error=str(e),
            error_type=type(e).__name__,
        )
        return []

    logger.info(
        "cloudmap_discovery_complete",
        namespace=namespace,
        agents_found=len(endpoints),
        agent_names=[e.name for e in endpoints],
    )
    return endpoints


async def _find_namespace_id(client, namespace_name: str) -> Optional[str]:
    """Find the CloudMap namespace ID by name.

    Args:
        client: servicediscovery aiobotocore client
        namespace_name: Namespace name to look up

    Returns:
        Namespace ID string, or None if not found.
    """
    paginator = client.get_paginator("list_namespaces")
    async for page in paginator.paginate():
        for ns in page.get("Namespaces", []):
            if ns["Name"] == namespace_name:
                return ns["Id"]
    return None


async def _list_services(client, namespace_id: str) -> List[str]:
    """List all service names in a CloudMap namespace.

    Args:
        client: servicediscovery aiobotocore client
        namespace_id: CloudMap namespace ID

    Returns:
        List of service names.
    """
    service_names: List[str] = []
    paginator = client.get_paginator("list_services")
    async for page in paginator.paginate(
        Filters=[
            {
                "Name": "NAMESPACE_ID",
                "Values": [namespace_id],
                "Condition": "EQ",
            }
        ]
    ):
        for svc in page.get("Services", []):
            service_names.append(svc["Name"])
    return service_names


async def _discover_service_instances(
    client,
    namespace_name: str,
    service_name: str,
) -> List[AgentEndpoint]:
    """Discover healthy instances for a single CloudMap service.

    Args:
        client: servicediscovery aiobotocore client
        namespace_name: CloudMap namespace name
        service_name: Service name to discover

    Returns:
        List of AgentEndpoint for healthy instances.
    """
    response = await client.discover_instances(
        NamespaceName=namespace_name,
        ServiceName=service_name,
        HealthStatus="HEALTHY",
        MaxResults=10,
    )

    endpoints: List[AgentEndpoint] = []
    for instance in response.get("Instances", []):
        attrs = instance.get("Attributes", {})
        ip = attrs.get("AWS_INSTANCE_IPV4")
        port = attrs.get("AWS_INSTANCE_PORT", "8000")

        if not ip:
            logger.warning(
                "cloudmap_instance_no_ip",
                service=service_name,
                instance_id=instance.get("InstanceId"),
            )
            continue

        url = f"http://{ip}:{port}"
        endpoints.append(
            AgentEndpoint(
                name=service_name,
                url=url,
                instance_id=instance.get("InstanceId"),
            )
        )

    return endpoints
