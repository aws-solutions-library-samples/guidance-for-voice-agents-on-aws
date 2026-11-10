"""Tests for AgentRegistry with background polling."""

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from app.a2a.discovery import AgentEndpoint
    from app.a2a.registry import (
        AgentEntry,
        AgentRegistry,
        AgentSkillInfo,
        _skill_to_bedrock_tool_spec,
    )
except ImportError:
    pytest.skip(
        "Container-only dependencies not available (structlog/aiobotocore)",
        allow_module_level=True,
    )


def _make_skill(
    skill_id: str = "search_knowledge_base",
    name: str = "search_knowledge_base",
    description: str = "Search the knowledge base for information.",
    agent_name: str = "KB Agent",
    agent_url: str = "http://10.0.1.5:8080",
) -> AgentSkillInfo:
    return AgentSkillInfo(
        skill_id=skill_id,
        skill_name=name,
        description=description,
        agent_name=agent_name,
        agent_url=agent_url,
    )


def _make_mock_agent_card(
    name: str = "KB Agent",
    description: str = "A knowledge base agent",
    skills: list | None = None,
):
    """Create a mock Agent Card matching Strands SDK structure."""
    card = MagicMock()
    card.name = name
    card.description = description

    if skills is None:
        skill = MagicMock()
        skill.id = "search_knowledge_base"
        skill.name = "search_knowledge_base"
        skill.description = "Search the knowledge base for information."
        skill.tags = []
        card.skills = [skill]
    else:
        card.skills = skills

    return card


class TestSkillToBedrockToolSpec:
    """Tests for _skill_to_bedrock_tool_spec."""

    def test_basic_conversion(self):
        skill = _make_skill()
        spec = _skill_to_bedrock_tool_spec(skill)

        assert "toolSpec" in spec
        tool_spec = spec["toolSpec"]
        assert tool_spec["name"] == "search_knowledge_base"
        assert tool_spec["description"] == "Search the knowledge base for information."

        # Check input schema has query parameter
        schema = tool_spec["inputSchema"]["json"]
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert schema["properties"]["query"]["type"] == "string"
        assert schema["required"] == ["query"]

    def test_preserves_full_description(self):
        long_desc = "A very long description " * 50
        skill = _make_skill(description=long_desc)
        spec = _skill_to_bedrock_tool_spec(skill)
        assert spec["toolSpec"]["description"] == long_desc


class TestAgentRegistry:
    """Tests for AgentRegistry."""

    def test_init(self):
        registry = AgentRegistry(namespace="test-ns")
        assert registry.namespace == "test-ns"
        assert registry.get_skill_count() == 0
        assert registry.get_agent_count() == 0
        assert not registry.is_polling

    @pytest.mark.asyncio
    async def test_refresh_discovers_agents(self):
        """Test that refresh() discovers agents and populates routing table."""
        registry = AgentRegistry(namespace="test-ns")

        # Mock discover_agents
        mock_endpoints = [
            AgentEndpoint(name="kb-agent", url="http://10.0.1.5:8080"),
        ]

        mock_card = _make_mock_agent_card()
        mock_a2a_agent = AsyncMock()
        mock_a2a_agent.get_agent_card.return_value = mock_card

        with (
            patch(
                "app.a2a.registry.discover_agents", new_callable=AsyncMock
            ) as mock_discover,
            patch("app.a2a.registry._get_a2a_agent_class") as mock_cls,
        ):
            mock_discover.return_value = mock_endpoints
            mock_cls.return_value = lambda endpoint, timeout: mock_a2a_agent

            await registry.refresh()

        assert registry.get_skill_count() == 1
        assert registry.get_agent_count() == 1

        entry = registry.get_agent_for_skill("search_knowledge_base")
        assert entry is not None
        assert entry.agent_name == "KB Agent"

    @pytest.mark.asyncio
    async def test_refresh_caches_agents(self):
        """Test that unchanged endpoints reuse cached A2AAgent instances."""
        registry = AgentRegistry(namespace="test-ns")

        mock_endpoints = [
            AgentEndpoint(name="kb-agent", url="http://10.0.1.5:8080"),
        ]

        mock_card = _make_mock_agent_card()
        mock_a2a_agent = AsyncMock()
        mock_a2a_agent.get_agent_card.return_value = mock_card

        create_count = 0

        def mock_create(endpoint, timeout):
            nonlocal create_count
            create_count += 1
            return mock_a2a_agent

        with (
            patch(
                "app.a2a.registry.discover_agents", new_callable=AsyncMock
            ) as mock_discover,
            patch("app.a2a.registry._get_a2a_agent_class") as mock_cls,
        ):
            mock_discover.return_value = mock_endpoints
            mock_cls.return_value = mock_create

            # First refresh — creates agent
            await registry.refresh()
            assert create_count == 1

            # Second refresh — same endpoint, should reuse cache
            await registry.refresh()
            assert create_count == 1  # NOT 2

    @pytest.mark.asyncio
    async def test_refresh_removes_stale_agents(self):
        """Test that agents are removed after the empty-discovery grace period.

        The registry carries forward cached agents for EMPTY_POLL_GRACE_COUNT
        consecutive empty polls before removing them.
        """
        registry = AgentRegistry(namespace="test-ns")

        mock_card = _make_mock_agent_card()
        mock_a2a_agent = AsyncMock()
        mock_a2a_agent.get_agent_card.return_value = mock_card

        with (
            patch(
                "app.a2a.registry.discover_agents", new_callable=AsyncMock
            ) as mock_discover,
            patch("app.a2a.registry._get_a2a_agent_class") as mock_cls,
        ):
            mock_cls.return_value = lambda endpoint, timeout: mock_a2a_agent

            # First refresh — one agent
            mock_discover.return_value = [
                AgentEndpoint(name="kb-agent", url="http://10.0.1.5:8080"),
            ]
            await registry.refresh()
            assert registry.get_skill_count() == 1

            # Empty polls within grace period — agents are carried forward
            mock_discover.return_value = []
            grace_count = AgentRegistry.EMPTY_POLL_GRACE_COUNT

            for i in range(grace_count - 1):
                await registry.refresh()
                assert registry.get_skill_count() == 1  # still carried forward

            # Next empty poll exhausts grace — agents removed
            await registry.refresh()
            assert registry.get_skill_count() == 0
            assert registry.get_agent_count() == 0

    @pytest.mark.asyncio
    async def test_refresh_handles_card_fetch_failure(self):
        """Test that a failing card fetch doesn't break other agents."""
        registry = AgentRegistry(namespace="test-ns")

        good_card = _make_mock_agent_card(name="Good Agent")

        call_count = 0

        def mock_create(endpoint, timeout):
            nonlocal call_count
            call_count += 1
            agent = AsyncMock()
            if endpoint == "http://10.0.1.5:8080":
                agent.get_agent_card.side_effect = Exception("Connection refused")
            else:
                agent.get_agent_card.return_value = good_card
            return agent

        with (
            patch(
                "app.a2a.registry.discover_agents", new_callable=AsyncMock
            ) as mock_discover,
            patch("app.a2a.registry._get_a2a_agent_class") as mock_cls,
        ):
            mock_discover.return_value = [
                AgentEndpoint(name="bad-agent", url="http://10.0.1.5:8080"),
                AgentEndpoint(name="good-agent", url="http://10.0.2.5:8080"),
            ]
            mock_cls.return_value = mock_create

            await registry.refresh()

        # Only the good agent should be registered
        assert registry.get_agent_count() == 1
        assert registry.get_skill_count() == 1

    @pytest.mark.asyncio
    async def test_refresh_handles_duplicate_skills(self):
        """Test that duplicate skill IDs across agents are handled."""
        registry = AgentRegistry(namespace="test-ns")

        card_a = _make_mock_agent_card(name="Agent A")
        card_b = _make_mock_agent_card(name="Agent B")

        def mock_create(endpoint, timeout):
            agent = AsyncMock()
            if "10.0.1.5" in endpoint:
                agent.get_agent_card.return_value = card_a
            else:
                agent.get_agent_card.return_value = card_b
            return agent

        with (
            patch(
                "app.a2a.registry.discover_agents", new_callable=AsyncMock
            ) as mock_discover,
            patch("app.a2a.registry._get_a2a_agent_class") as mock_cls,
        ):
            mock_discover.return_value = [
                AgentEndpoint(name="agent-a", url="http://10.0.1.5:8080"),
                AgentEndpoint(name="agent-b", url="http://10.0.2.5:8080"),
            ]
            mock_cls.return_value = mock_create

            await registry.refresh()

        # Both agents should be registered but only one skill entry (last wins)
        assert registry.get_agent_count() == 2
        assert registry.get_skill_count() == 1

    def test_get_tool_definitions(self):
        """Test that tool definitions are generated correctly."""
        registry = AgentRegistry(namespace="test-ns")

        # Manually set up routing table
        skill = _make_skill()
        entry = AgentEntry(
            agent=MagicMock(),
            endpoint=AgentEndpoint(name="kb-agent", url="http://10.0.1.5:8080"),
            agent_name="KB Agent",
            agent_description="A knowledge base agent",
            skills=[skill],
        )
        registry._agent_cache = {"http://10.0.1.5:8080": entry}
        registry._skill_table = {"search_knowledge_base": entry}

        tool_defs = registry.get_tool_definitions()

        assert len(tool_defs) == 1
        assert tool_defs[0]["toolSpec"]["name"] == "search_knowledge_base"
        assert "query" in tool_defs[0]["toolSpec"]["inputSchema"]["json"]["properties"]

    def test_get_all_skills(self):
        """Test retrieving all skill metadata."""
        registry = AgentRegistry(namespace="test-ns")

        skill1 = _make_skill(skill_id="search_kb", name="search_kb")
        skill2 = _make_skill(skill_id="lookup_customer", name="lookup_customer")

        entry1 = AgentEntry(
            agent=MagicMock(),
            endpoint=AgentEndpoint(name="kb-agent", url="http://10.0.1.5:8080"),
            agent_name="KB Agent",
            agent_description="KB",
            skills=[skill1],
        )
        entry2 = AgentEntry(
            agent=MagicMock(),
            endpoint=AgentEndpoint(name="crm-agent", url="http://10.0.2.5:8080"),
            agent_name="CRM Agent",
            agent_description="CRM",
            skills=[skill2],
        )
        registry._agent_cache = {
            "http://10.0.1.5:8080": entry1,
            "http://10.0.2.5:8080": entry2,
        }

        skills = registry.get_all_skills()
        assert len(skills) == 2
        skill_ids = {s.skill_id for s in skills}
        assert skill_ids == {"search_kb", "lookup_customer"}

    @pytest.mark.asyncio
    async def test_start_stop_polling(self):
        """Test polling lifecycle."""
        registry = AgentRegistry(namespace="test-ns")

        with patch(
            "app.a2a.registry.discover_agents", new_callable=AsyncMock
        ) as mock_discover:
            mock_discover.return_value = []

            await registry.start_polling(interval_seconds=60)
            assert registry.is_polling

            # Allow initial refresh to run
            await asyncio.sleep(0.1)

            await registry.stop_polling()
            assert not registry.is_polling

    @pytest.mark.asyncio
    async def test_start_polling_double_start(self):
        """Test that double start is a no-op."""
        registry = AgentRegistry(namespace="test-ns")

        with patch(
            "app.a2a.registry.discover_agents", new_callable=AsyncMock
        ) as mock_discover:
            mock_discover.return_value = []

            await registry.start_polling(interval_seconds=60)
            await registry.start_polling(
                interval_seconds=60
            )  # Should warn but not fail

            await asyncio.sleep(0.1)
            await registry.stop_polling()

    def test_get_agent_for_skill_not_found(self):
        registry = AgentRegistry(namespace="test-ns")
        assert registry.get_agent_for_skill("nonexistent") is None

    def test_get_tool_definitions_empty(self):
        registry = AgentRegistry(namespace="test-ns")
        assert registry.get_tool_definitions() == []
