"""Tests for CloudMap service discovery."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from app.a2a.discovery import (
        AgentEndpoint,
        discover_agents,
        _find_namespace_id,
        _list_services,
        _discover_service_instances,
    )
except ImportError:
    pytest.skip(
        "Container-only dependencies not available (structlog/aiobotocore)",
        allow_module_level=True,
    )


def _make_paginator(pages_fn):
    """Create a mock paginator that works with aiobotocore's async for pattern.

    aiobotocore's get_paginator() is sync, but paginator.paginate() returns
    an async iterator. We need MagicMock (not AsyncMock) for get_paginator.
    """
    paginator = MagicMock()
    paginator.paginate.return_value = pages_fn()
    return paginator


class TestAgentEndpoint:
    """Tests for AgentEndpoint dataclass."""

    def test_basic_creation(self):
        ep = AgentEndpoint(name="kb-agent", url="http://10.0.1.5:8080")
        assert ep.name == "kb-agent"
        assert ep.url == "http://10.0.1.5:8080"
        assert ep.instance_id is None

    def test_with_instance_id(self):
        ep = AgentEndpoint(
            name="crm-agent",
            url="http://10.0.2.3:8080",
            instance_id="i-abc123",
        )
        assert ep.instance_id == "i-abc123"


class TestFindNamespaceId:
    """Tests for _find_namespace_id helper."""

    @pytest.mark.asyncio
    async def test_finds_namespace(self):
        client = MagicMock()

        async def async_pages():
            yield {
                "Namespaces": [
                    {"Name": "other-ns", "Id": "ns-111"},
                    {"Name": "voice-agent-capabilities", "Id": "ns-222"},
                ]
            }

        client.get_paginator.return_value = _make_paginator(async_pages)

        result = await _find_namespace_id(client, "voice-agent-capabilities")
        assert result == "ns-222"

    @pytest.mark.asyncio
    async def test_namespace_not_found(self):
        client = MagicMock()

        async def async_pages():
            yield {"Namespaces": [{"Name": "other-ns", "Id": "ns-111"}]}

        client.get_paginator.return_value = _make_paginator(async_pages)

        result = await _find_namespace_id(client, "nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_namespaces(self):
        client = MagicMock()

        async def async_pages():
            yield {"Namespaces": []}

        client.get_paginator.return_value = _make_paginator(async_pages)

        result = await _find_namespace_id(client, "voice-agent-capabilities")
        assert result is None


class TestListServices:
    """Tests for _list_services helper."""

    @pytest.mark.asyncio
    async def test_lists_services(self):
        client = MagicMock()

        async def async_pages():
            yield {
                "Services": [
                    {"Name": "kb-agent"},
                    {"Name": "crm-agent"},
                ]
            }

        client.get_paginator.return_value = _make_paginator(async_pages)

        result = await _list_services(client, "ns-222")
        assert result == ["kb-agent", "crm-agent"]

    @pytest.mark.asyncio
    async def test_empty_services(self):
        client = MagicMock()

        async def async_pages():
            yield {"Services": []}

        client.get_paginator.return_value = _make_paginator(async_pages)

        result = await _list_services(client, "ns-222")
        assert result == []


class TestDiscoverServiceInstances:
    """Tests for _discover_service_instances helper."""

    @pytest.mark.asyncio
    async def test_discovers_healthy_instances(self):
        client = AsyncMock()
        client.discover_instances.return_value = {
            "Instances": [
                {
                    "InstanceId": "i-abc",
                    "Attributes": {
                        "AWS_INSTANCE_IPV4": "10.0.1.5",
                        "AWS_INSTANCE_PORT": "8080",
                    },
                },
                {
                    "InstanceId": "i-def",
                    "Attributes": {
                        "AWS_INSTANCE_IPV4": "10.0.1.6",
                        "AWS_INSTANCE_PORT": "9000",
                    },
                },
            ]
        }

        result = await _discover_service_instances(
            client, "voice-agent-capabilities", "kb-agent"
        )

        assert len(result) == 2
        assert result[0].name == "kb-agent"
        assert result[0].url == "http://10.0.1.5:8080"
        assert result[0].instance_id == "i-abc"
        assert result[1].url == "http://10.0.1.6:9000"

    @pytest.mark.asyncio
    async def test_skips_instances_without_ip(self):
        client = AsyncMock()
        client.discover_instances.return_value = {
            "Instances": [
                {
                    "InstanceId": "i-no-ip",
                    "Attributes": {"AWS_INSTANCE_PORT": "8080"},
                },
                {
                    "InstanceId": "i-has-ip",
                    "Attributes": {
                        "AWS_INSTANCE_IPV4": "10.0.1.5",
                        "AWS_INSTANCE_PORT": "8080",
                    },
                },
            ]
        }

        result = await _discover_service_instances(
            client, "voice-agent-capabilities", "kb-agent"
        )

        assert len(result) == 1
        assert result[0].instance_id == "i-has-ip"

    @pytest.mark.asyncio
    async def test_default_port(self):
        client = AsyncMock()
        client.discover_instances.return_value = {
            "Instances": [
                {
                    "InstanceId": "i-abc",
                    "Attributes": {"AWS_INSTANCE_IPV4": "10.0.1.5"},
                },
            ]
        }

        result = await _discover_service_instances(
            client, "voice-agent-capabilities", "kb-agent"
        )

        assert result[0].url == "http://10.0.1.5:8000"

    @pytest.mark.asyncio
    async def test_no_instances(self):
        client = AsyncMock()
        client.discover_instances.return_value = {"Instances": []}

        result = await _discover_service_instances(
            client, "voice-agent-capabilities", "kb-agent"
        )

        assert result == []


class TestDiscoverAgents:
    """Integration-style tests for the main discover_agents function."""

    @pytest.mark.asyncio
    async def test_discovers_agents_end_to_end(self):
        """Test full discovery flow with mocked boto3."""
        mock_client = MagicMock()

        # Mock namespace lookup
        async def ns_pages():
            yield {"Namespaces": [{"Name": "voice-agent-capabilities", "Id": "ns-222"}]}

        # Mock service listing
        async def svc_pages():
            yield {"Services": [{"Name": "kb-agent"}]}

        # Route paginator calls
        def get_paginator(name):
            if name == "list_namespaces":
                return _make_paginator(ns_pages)
            elif name == "list_services":
                return _make_paginator(svc_pages)
            raise ValueError(f"Unknown paginator: {name}")

        mock_client.get_paginator = get_paginator

        # Mock instance discovery (async method)
        mock_client.discover_instances = AsyncMock(
            return_value={
                "Instances": [
                    {
                        "InstanceId": "i-abc",
                        "Attributes": {
                            "AWS_INSTANCE_IPV4": "10.0.1.5",
                            "AWS_INSTANCE_PORT": "8080",
                        },
                    }
                ]
            }
        )

        # Mock aiobotocore session
        mock_session = MagicMock()

        class MockContextManager:
            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        mock_session.create_client.return_value = MockContextManager()

        result = await discover_agents(
            "voice-agent-capabilities",
            session=mock_session,
        )

        assert len(result) == 1
        assert result[0].name == "kb-agent"
        assert result[0].url == "http://10.0.1.5:8080"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_namespace(self):
        mock_client = MagicMock()

        async def empty_pages():
            yield {"Namespaces": []}

        mock_client.get_paginator.return_value = _make_paginator(empty_pages)

        mock_session = MagicMock()

        class MockContextManager:
            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        mock_session.create_client.return_value = MockContextManager()

        result = await discover_agents("nonexistent-ns", session=mock_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        mock_session = MagicMock()

        class MockContextManager:
            async def __aenter__(self):
                raise Exception("Connection error")

            async def __aexit__(self, *args):
                pass

        mock_session.create_client.return_value = MockContextManager()

        result = await discover_agents("voice-agent-capabilities", session=mock_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_continues_on_per_service_error(self):
        """If one service fails discovery, others should still be returned."""
        mock_client = MagicMock()

        # Namespace
        async def ns_pages():
            yield {"Namespaces": [{"Name": "ns", "Id": "ns-1"}]}

        # Services
        async def svc_pages():
            yield {"Services": [{"Name": "failing-agent"}, {"Name": "good-agent"}]}

        def get_paginator(name):
            if name == "list_namespaces":
                return _make_paginator(ns_pages)
            elif name == "list_services":
                return _make_paginator(svc_pages)
            raise ValueError(f"Unknown paginator: {name}")

        mock_client.get_paginator = get_paginator

        # First call fails, second succeeds
        async def mock_discover_instances(**kwargs):
            if kwargs["ServiceName"] == "failing-agent":
                raise Exception("Discovery failed")
            return {
                "Instances": [
                    {
                        "InstanceId": "i-good",
                        "Attributes": {
                            "AWS_INSTANCE_IPV4": "10.0.1.5",
                            "AWS_INSTANCE_PORT": "8080",
                        },
                    }
                ]
            }

        mock_client.discover_instances = mock_discover_instances

        mock_session = MagicMock()

        class MockContextManager:
            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        mock_session.create_client.return_value = MockContextManager()

        result = await discover_agents("ns", session=mock_session)

        assert len(result) == 1
        assert result[0].name == "good-agent"
