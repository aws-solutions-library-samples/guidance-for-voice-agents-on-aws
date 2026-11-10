"""Integration tests for A2A capability registry pipeline wiring.

Tests _register_capabilities(), create_voice_pipeline() with A2A registry,
service_main lifecycle, and config_service A2A config.

Run with: pytest tests/test_a2a_pipeline_integration.py -v
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from app.a2a.discovery import AgentEndpoint
    from app.a2a.registry import AgentEntry, AgentRegistry, AgentSkillInfo
    from pipecat.adapters.schemas.function_schema import FunctionSchema
except ImportError:
    pytest.skip(
        "Container-only dependencies not available (structlog/aiobotocore)",
        allow_module_level=True,
    )


# ============================================================
# Helpers
# ============================================================


def _make_function_schema(
    name: str = "time",
    description: str = "Get time",
    properties: dict | None = None,
    required: list | None = None,
) -> FunctionSchema:
    """Create a FunctionSchema matching the new _register_tools return type."""
    return FunctionSchema(
        name=name,
        description=description,
        properties=properties or {},
        required=required or [],
    )


# ============================================================
# Helpers
# ============================================================


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


def _make_entry(
    skill_id: str = "remote_kb_search",
    agent_name: str = "KB Agent",
    url: str = "http://10.0.1.5:8080",
) -> AgentEntry:
    """Create a mock AgentEntry with an A2AAgent-like mock."""
    agent = AsyncMock()
    endpoint = AgentEndpoint(name=agent_name, url=url, instance_id="i-abc123")
    skill = _make_skill(
        skill_id=skill_id, name=skill_id, agent_name=agent_name, agent_url=url
    )
    return AgentEntry(
        agent=agent,
        endpoint=endpoint,
        agent_name=agent_name,
        agent_description="A test agent",
        skills=[skill],
    )


def _make_registry_with_skills(
    skills: list[tuple[str, str]] | None = None,
) -> MagicMock:
    """Create a mock AgentRegistry with specified skills.

    Args:
        skills: List of (skill_id, agent_name) tuples. Defaults to one remote skill.
    """
    if skills is None:
        skills = [("remote_kb_search", "KB Agent")]

    registry = MagicMock(spec=AgentRegistry)

    all_skills = []
    entries = {}
    tool_defs = []

    for skill_id, agent_name in skills:
        entry = _make_entry(skill_id=skill_id, agent_name=agent_name)
        skill_info = entry.skills[0]
        all_skills.append(skill_info)
        entries[skill_id] = entry
        tool_defs.append(
            {
                "toolSpec": {
                    "name": skill_id,
                    "description": f"Description for {skill_id}",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "Natural language query.",
                                }
                            },
                            "required": ["query"],
                        }
                    },
                }
            }
        )

    registry.get_all_skills.return_value = all_skills
    registry.get_tool_definitions.return_value = tool_defs
    registry.get_agent_for_skill.side_effect = lambda sid: entries.get(sid)
    registry.get_skill_count.return_value = len(skills)
    registry.get_agent_count.return_value = len(set(n for _, n in skills))
    registry.a2a_timeout = 30

    return registry


# ============================================================
# Tests: _register_capabilities()
# ============================================================


class TestRegisterCapabilities:
    """Tests for _register_capabilities() in pipeline_ecs."""

    @patch("app.pipeline_ecs._register_tools")
    @patch("app.pipeline_ecs._get_config")
    def test_merges_local_and_remote_tools(self, mock_config, mock_register_tools):
        """Remote A2A tools are appended to local tools."""
        from app.pipeline_ecs import _register_capabilities

        # Mock local tools (now FunctionSchema objects)
        local_tools = [_make_function_schema(name="time", description="Get time")]
        mock_register_tools.return_value = local_tools

        # Mock config
        config = MagicMock()
        config.features.enable_tool_calling = True
        mock_config.return_value = config

        # Mock registry with one remote skill
        registry = _make_registry_with_skills([("remote_search", "Remote Agent")])

        llm = MagicMock()
        result = _register_capabilities(
            llm=llm,
            session_id="test-123",
            transport=MagicMock(),
            collector=None,
            sip_session_tracker=None,
            a2a_registry=registry,
        )

        # Should have local + remote tools (all FunctionSchema now)
        assert len(result) == 2
        tool_names = [t.name for t in result]
        assert "time" in tool_names
        assert "remote_search" in tool_names

        # A2A handler should be registered with LLM
        llm.register_function.assert_called_once()
        call_kwargs = llm.register_function.call_args
        assert call_kwargs[1]["function_name"] == "remote_search"

    @patch("app.pipeline_ecs._register_tools")
    @patch("app.pipeline_ecs._get_config")
    def test_local_tools_shadow_remote_conflicts(
        self, mock_config, mock_register_tools
    ):
        """Local tools take precedence when skill_id conflicts."""
        from app.pipeline_ecs import _register_capabilities

        # Local tool named "search_knowledge_base"
        local_tools = [
            _make_function_schema(
                name="search_knowledge_base", description="Local KB search"
            )
        ]
        mock_register_tools.return_value = local_tools

        config = MagicMock()
        config.features.enable_tool_calling = True
        mock_config.return_value = config

        # Remote skill with same name
        registry = _make_registry_with_skills([("search_knowledge_base", "KB Agent")])

        llm = MagicMock()
        result = _register_capabilities(
            llm=llm,
            session_id="test-123",
            transport=MagicMock(),
            a2a_registry=registry,
        )

        # Should only have the local tool (remote is shadowed)
        assert len(result) == 1
        assert result[0].name == "search_knowledge_base"
        assert result[0].description == "Local KB search"

        # A2A handler should NOT be registered
        llm.register_function.assert_not_called()

    @patch("app.pipeline_ecs._register_tools")
    @patch("app.pipeline_ecs._get_config")
    def test_no_registry_returns_local_tools_only(
        self, mock_config, mock_register_tools
    ):
        """When a2a_registry is None, only local tools are returned."""
        from app.pipeline_ecs import _register_capabilities

        local_tools = [_make_function_schema(name="time", description="Get time")]
        mock_register_tools.return_value = local_tools

        config = MagicMock()
        mock_config.return_value = config

        llm = MagicMock()
        result = _register_capabilities(
            llm=llm,
            session_id="test-123",
            transport=MagicMock(),
            a2a_registry=None,
        )

        assert result == local_tools

    @patch("app.pipeline_ecs._register_tools")
    @patch("app.pipeline_ecs._get_config")
    def test_empty_registry_returns_local_tools(self, mock_config, mock_register_tools):
        """When registry has no skills, only local tools are returned."""
        from app.pipeline_ecs import _register_capabilities

        local_tools = [_make_function_schema(name="time", description="Get time")]
        mock_register_tools.return_value = local_tools

        config = MagicMock()
        mock_config.return_value = config

        registry = _make_registry_with_skills([])

        llm = MagicMock()
        result = _register_capabilities(
            llm=llm,
            session_id="test-123",
            transport=MagicMock(),
            a2a_registry=registry,
        )

        assert result == local_tools

    @patch("app.pipeline_ecs._register_tools")
    @patch("app.pipeline_ecs._get_config")
    def test_multiple_remote_skills(self, mock_config, mock_register_tools):
        """Multiple remote skills are all registered."""
        from app.pipeline_ecs import _register_capabilities

        local_tools = [_make_function_schema(name="time", description="Get time")]
        mock_register_tools.return_value = local_tools

        config = MagicMock()
        mock_config.return_value = config

        registry = _make_registry_with_skills(
            [
                ("remote_kb_search", "KB Agent"),
                ("remote_crm_lookup", "CRM Agent"),
            ]
        )

        llm = MagicMock()
        result = _register_capabilities(
            llm=llm,
            session_id="test-123",
            transport=MagicMock(),
            collector=MagicMock(),
            a2a_registry=registry,
        )

        assert len(result) == 3
        tool_names = [t.name for t in result]
        assert "time" in tool_names
        assert "remote_kb_search" in tool_names
        assert "remote_crm_lookup" in tool_names

        # Two A2A handlers registered
        assert llm.register_function.call_count == 2

    @patch("app.pipeline_ecs._register_tools")
    @patch("app.pipeline_ecs._get_config")
    def test_passes_collector_to_a2a_handler(self, mock_config, mock_register_tools):
        """Metrics collector is passed through to A2A tool handlers."""
        from app.pipeline_ecs import _register_capabilities

        local_tools: list = []
        mock_register_tools.return_value = local_tools

        config = MagicMock()
        mock_config.return_value = config

        registry = _make_registry_with_skills([("remote_search", "Agent")])
        collector = MagicMock()

        llm = MagicMock()
        with patch("app.a2a.create_a2a_tool_handler") as mock_create:
            mock_create.return_value = AsyncMock()
            _register_capabilities(
                llm=llm,
                session_id="test-123",
                transport=MagicMock(),
                collector=collector,
                a2a_registry=registry,
            )

            mock_create.assert_called_once_with(
                skill_id="remote_search",
                agent=registry.get_agent_for_skill("remote_search").agent,
                timeout_seconds=30.0,
                collector=collector,
            )


# ============================================================
# Tests: Config A2A integration
# ============================================================


class TestA2AConfig:
    """Tests for A2A config in AppConfig/FeatureFlags."""

    def test_feature_flag_defaults_to_false(self):
        """enable_capability_registry defaults to False."""
        from app.services.config_service import FeatureFlags

        flags = FeatureFlags()
        assert flags.enable_capability_registry is False

    def test_a2a_config_defaults(self):
        """A2AConfig has sensible defaults."""
        from app.services.config_service import A2AConfig

        config = A2AConfig()
        assert config.namespace == ""
        assert config.poll_interval_seconds == 30
        assert config.tool_timeout_seconds == 30

    def test_app_config_includes_a2a(self):
        """AppConfig includes a2a field."""
        from app.services.config_service import AppConfig, A2AConfig

        config = AppConfig()
        assert isinstance(config.a2a, A2AConfig)
        assert config.a2a.namespace == ""

    def test_build_config_reads_a2a_params(self):
        """ConfigService._build_config reads A2A SSM parameters."""
        from app.services.config_service import ConfigService

        service = ConfigService.__new__(ConfigService)
        service.region = "us-east-1"
        service.KNOWLEDGE_BASE_PATH = "/voice-agent/knowledge-base"
        service.CONFIG_PATH = "/voice-agent/config"
        service.SESSIONS_PATH = "/voice-agent/sessions"
        service.STORAGE_PATH = "/voice-agent/storage"
        service.A2A_PATH = "/voice-agent/a2a"

        params = {
            "/voice-agent/config/enable-capability-registry": "true",
            "/voice-agent/a2a/namespace": "my-agents",
            "/voice-agent/a2a/poll-interval-seconds": "60",
            "/voice-agent/a2a/tool-timeout-seconds": "15",
        }

        config = service._build_config(params)

        assert config.features.enable_capability_registry is True
        assert config.a2a.namespace == "my-agents"
        assert config.a2a.poll_interval_seconds == 60
        assert config.a2a.tool_timeout_seconds == 15

    def test_build_config_a2a_defaults_when_missing(self):
        """A2A config uses defaults when SSM params not set."""
        from app.services.config_service import ConfigService

        service = ConfigService.__new__(ConfigService)
        service.region = "us-east-1"
        service.KNOWLEDGE_BASE_PATH = "/voice-agent/knowledge-base"
        service.CONFIG_PATH = "/voice-agent/config"
        service.SESSIONS_PATH = "/voice-agent/sessions"
        service.STORAGE_PATH = "/voice-agent/storage"
        service.A2A_PATH = "/voice-agent/a2a"

        config = service._build_config({})

        assert config.features.enable_capability_registry is False
        assert config.a2a.namespace == ""
        assert config.a2a.poll_interval_seconds == 30
        assert config.a2a.tool_timeout_seconds == 30

    def test_a2a_namespace_env_var_fallback(self):
        """A2A namespace falls back to A2A_NAMESPACE env var."""
        from app.services.config_service import ConfigService

        service = ConfigService.__new__(ConfigService)
        service.region = "us-east-1"
        service.KNOWLEDGE_BASE_PATH = "/voice-agent/knowledge-base"
        service.CONFIG_PATH = "/voice-agent/config"
        service.SESSIONS_PATH = "/voice-agent/sessions"
        service.STORAGE_PATH = "/voice-agent/storage"
        service.A2A_PATH = "/voice-agent/a2a"

        with patch.dict(os.environ, {"A2A_NAMESPACE": "env-namespace"}):
            config = service._build_config({})

        assert config.a2a.namespace == "env-namespace"


# ============================================================
# Tests: Pipeline feature flag routing
# ============================================================


class TestPipelineFeatureFlagRouting:
    """Tests for create_voice_pipeline tool calling path selection."""

    @patch("app.pipeline_ecs._get_enable_tool_calling", return_value=True)
    @patch("app.pipeline_ecs._get_enable_capability_registry", return_value=True)
    @patch("app.pipeline_ecs._register_capabilities")
    @patch("app.pipeline_ecs._get_enable_filler_phrases", return_value=False)
    @patch("app.pipeline_ecs._get_enable_audio_quality", return_value=False)
    @patch("app.pipeline_ecs._get_enable_conversation_logging", return_value=False)
    @patch("app.pipeline_ecs._get_llm_model_id", return_value="test-model")
    @patch("app.pipeline_ecs.DailyTransport")
    @patch("app.pipeline_ecs.DailyParams")
    @patch("app.pipeline_ecs.AWSBedrockLLMService")
    @patch("app.pipeline_ecs.SileroVADAnalyzer")
    @patch("app.services.factory.create_stt_service")
    @patch("app.services.factory.create_tts_service")
    @patch("app.pipeline_ecs.LLMContextAggregatorPair")
    @patch("app.pipeline_ecs._get_config")
    @pytest.mark.asyncio
    async def test_uses_register_capabilities_when_flag_enabled(
        self,
        mock_get_config,
        mock_aggregator_pair,
        mock_tts_factory,
        mock_stt_factory,
        mock_vad,
        mock_bedrock,
        mock_daily_params,
        mock_daily,
        mock_model_id,
        mock_conv_log,
        mock_audio_quality,
        mock_filler,
        mock_register_caps,
        mock_enable_registry,
        mock_enable_tools,
    ):
        """When both tool calling and capability registry are enabled, uses _register_capabilities."""
        from app.pipeline_ecs import PipelineConfig, create_voice_pipeline

        mock_register_caps.return_value = []

        # Create mocks for pipeline components
        mock_transport = MagicMock()
        mock_transport.input.return_value = MagicMock()
        mock_transport.output.return_value = MagicMock()
        mock_daily.return_value = mock_transport

        mock_llm = MagicMock()
        mock_bedrock.return_value = mock_llm

        mock_agg = MagicMock()
        mock_agg.user.return_value = MagicMock()
        mock_agg.assistant.return_value = MagicMock()
        mock_aggregator_pair.return_value = mock_agg

        mock_stt_factory.return_value = MagicMock()
        mock_tts_factory.return_value = MagicMock()

        registry = _make_registry_with_skills()

        config = PipelineConfig(
            room_url="https://test.daily.co/room",
            room_token="test-token",
            session_id="test-123",
            system_prompt="You are a test assistant.",
            voice_id="test-voice",
            aws_region="us-east-1",
        )

        with patch.dict(os.environ, {"DAILY_API_KEY": "test-key"}):
            await create_voice_pipeline(config, collector=None, a2a_registry=registry)

        # Should have called _register_capabilities (not _register_tools directly)
        mock_register_caps.assert_called_once()
        call_kwargs = mock_register_caps.call_args
        assert call_kwargs[1].get("a2a_registry") is registry or (
            len(call_kwargs[0]) >= 6 and call_kwargs[0][5] is registry
        )

    @patch("app.pipeline_ecs._get_enable_tool_calling", return_value=True)
    @patch("app.pipeline_ecs._get_enable_capability_registry", return_value=False)
    @patch("app.pipeline_ecs._register_tools")
    @patch("app.pipeline_ecs._get_enable_filler_phrases", return_value=False)
    @patch("app.pipeline_ecs._get_enable_audio_quality", return_value=False)
    @patch("app.pipeline_ecs._get_enable_conversation_logging", return_value=False)
    @patch("app.pipeline_ecs._get_llm_model_id", return_value="test-model")
    @patch("app.pipeline_ecs.DailyTransport")
    @patch("app.pipeline_ecs.DailyParams")
    @patch("app.pipeline_ecs.AWSBedrockLLMService")
    @patch("app.pipeline_ecs.SileroVADAnalyzer")
    @patch("app.services.factory.create_stt_service")
    @patch("app.services.factory.create_tts_service")
    @patch("app.pipeline_ecs.LLMContextAggregatorPair")
    @patch("app.pipeline_ecs._get_config")
    @pytest.mark.asyncio
    async def test_uses_register_tools_when_registry_disabled(
        self,
        mock_get_config,
        mock_aggregator_pair,
        mock_tts_factory,
        mock_stt_factory,
        mock_vad,
        mock_bedrock,
        mock_daily_params,
        mock_daily,
        mock_model_id,
        mock_conv_log,
        mock_audio_quality,
        mock_filler,
        mock_register_tools,
        mock_enable_registry,
        mock_enable_tools,
    ):
        """When capability registry is disabled, falls back to _register_tools."""
        from app.pipeline_ecs import PipelineConfig, create_voice_pipeline

        mock_register_tools.return_value = []

        mock_transport = MagicMock()
        mock_transport.input.return_value = MagicMock()
        mock_transport.output.return_value = MagicMock()
        mock_daily.return_value = mock_transport

        mock_llm = MagicMock()
        mock_bedrock.return_value = mock_llm

        mock_agg = MagicMock()
        mock_agg.user.return_value = MagicMock()
        mock_agg.assistant.return_value = MagicMock()
        mock_aggregator_pair.return_value = mock_agg

        mock_stt_factory.return_value = MagicMock()
        mock_tts_factory.return_value = MagicMock()

        registry = _make_registry_with_skills()

        config = PipelineConfig(
            room_url="https://test.daily.co/room",
            room_token="test-token",
            session_id="test-123",
            system_prompt="You are a test assistant.",
            voice_id="test-voice",
            aws_region="us-east-1",
        )

        with patch.dict(os.environ, {"DAILY_API_KEY": "test-key"}):
            await create_voice_pipeline(config, collector=None, a2a_registry=registry)

        # Should have called _register_tools (legacy path)
        mock_register_tools.assert_called_once()


# ============================================================
# Tests: Service main A2A lifecycle
# ============================================================


class TestServiceMainA2ALifecycle:
    """Tests for A2A registry lifecycle in service_main."""

    def test_registry_created_when_enabled(self):
        """AgentRegistry is created when feature flag and namespace are set."""
        from app.services.config_service import AppConfig, FeatureFlags, A2AConfig

        config = AppConfig(
            features=FeatureFlags(enable_capability_registry=True),
            a2a=A2AConfig(namespace="test-namespace", poll_interval_seconds=60),
        )

        import app.service_main as sm

        original_registry = sm._a2a_registry
        original_interval = sm._a2a_poll_interval

        try:
            with patch(
                "app.service_main.load_config", new_callable=AsyncMock
            ) as mock_load:
                mock_load.return_value = config

                with patch("app.a2a.AgentRegistry") as MockRegistry:
                    mock_instance = MagicMock()
                    MockRegistry.return_value = mock_instance

                    with patch("app.service_main.PipelineManager"):
                        with patch(
                            "app.service_main.load_secrets_from_aws", return_value=True
                        ):
                            with patch("asyncio.new_event_loop") as mock_loop:
                                with patch("asyncio.set_event_loop"):
                                    mock_loop_instance = MagicMock()
                                    mock_loop.return_value = mock_loop_instance
                                    mock_loop_instance.run_until_complete.side_effect = KeyboardInterrupt

                                    try:
                                        sm.main()
                                    except (KeyboardInterrupt, SystemExit):
                                        pass

                    MockRegistry.assert_called_once_with(
                        namespace="test-namespace",
                        region="us-east-1",
                        a2a_timeout=30,
                    )
                    assert sm._a2a_registry is mock_instance
                    assert sm._a2a_poll_interval == 60
        finally:
            sm._a2a_registry = original_registry
            sm._a2a_poll_interval = original_interval

    def test_registry_not_created_when_disabled(self):
        """AgentRegistry is NOT created when feature flag is False."""
        from app.services.config_service import AppConfig, FeatureFlags, A2AConfig

        config = AppConfig(
            features=FeatureFlags(enable_capability_registry=False),
            a2a=A2AConfig(namespace="test-namespace"),
        )

        import app.service_main as sm

        original_registry = sm._a2a_registry
        original_interval = sm._a2a_poll_interval

        try:
            sm._a2a_registry = None  # Explicitly reset

            with patch(
                "app.service_main.load_config", new_callable=AsyncMock
            ) as mock_load:
                mock_load.return_value = config

                with patch("app.service_main.PipelineManager"):
                    with patch(
                        "app.service_main.load_secrets_from_aws", return_value=True
                    ):
                        with patch("asyncio.new_event_loop") as mock_loop:
                            with patch("asyncio.set_event_loop"):
                                mock_loop_instance = MagicMock()
                                mock_loop.return_value = mock_loop_instance
                                mock_loop_instance.run_until_complete.side_effect = (
                                    KeyboardInterrupt
                                )

                                try:
                                    sm.main()
                                except (KeyboardInterrupt, SystemExit):
                                    pass

                assert sm._a2a_registry is None
        finally:
            sm._a2a_registry = original_registry
            sm._a2a_poll_interval = original_interval

    def test_registry_not_created_without_namespace(self):
        """AgentRegistry not created when enabled but namespace is empty."""
        from app.services.config_service import AppConfig, FeatureFlags, A2AConfig

        config = AppConfig(
            features=FeatureFlags(enable_capability_registry=True),
            a2a=A2AConfig(namespace=""),  # Empty namespace
        )

        import app.service_main as sm

        original_registry = sm._a2a_registry
        original_interval = sm._a2a_poll_interval

        try:
            sm._a2a_registry = None  # Explicitly reset

            with patch(
                "app.service_main.load_config", new_callable=AsyncMock
            ) as mock_load:
                mock_load.return_value = config

                with patch("app.service_main.PipelineManager"):
                    with patch(
                        "app.service_main.load_secrets_from_aws", return_value=True
                    ):
                        with patch("asyncio.new_event_loop") as mock_loop:
                            with patch("asyncio.set_event_loop"):
                                mock_loop_instance = MagicMock()
                                mock_loop.return_value = mock_loop_instance
                                mock_loop_instance.run_until_complete.side_effect = (
                                    KeyboardInterrupt
                                )

                                try:
                                    sm.main()
                                except (KeyboardInterrupt, SystemExit):
                                    pass

                assert sm._a2a_registry is None
        finally:
            sm._a2a_registry = original_registry
            sm._a2a_poll_interval = original_interval

    @pytest.mark.asyncio
    async def test_run_server_starts_registry_polling(self):
        """run_server() starts A2A registry polling."""
        import app.service_main as sm

        original_registry = sm._a2a_registry
        original_manager = sm.pipeline_manager
        original_interval = sm._a2a_poll_interval

        try:
            mock_registry = AsyncMock()
            sm._a2a_registry = mock_registry
            sm._a2a_poll_interval = 45

            mock_manager = MagicMock()
            mock_manager.start_heartbeat_loop = AsyncMock()
            sm.pipeline_manager = mock_manager

            with patch("app.service_main.web.AppRunner") as MockRunner:
                mock_runner = AsyncMock()
                MockRunner.return_value = mock_runner

                with patch("app.service_main.web.TCPSite") as MockSite:
                    mock_site = AsyncMock()
                    MockSite.return_value = mock_site

                    # Run server but cancel quickly
                    async def cancel_after_start(*args, **kwargs):
                        raise asyncio.CancelledError()

                    with patch("asyncio.sleep", side_effect=cancel_after_start):
                        try:
                            await sm.run_server(8080)
                        except asyncio.CancelledError:
                            pass

            mock_registry.start_polling.assert_called_once_with(interval_seconds=45)
            mock_registry.stop_polling.assert_called_once()
        finally:
            sm._a2a_registry = original_registry
            sm.pipeline_manager = original_manager
            sm._a2a_poll_interval = original_interval

    @pytest.mark.asyncio
    async def test_run_server_skips_registry_when_none(self):
        """run_server() handles None registry gracefully."""
        import app.service_main as sm

        original_registry = sm._a2a_registry
        original_manager = sm.pipeline_manager

        try:
            sm._a2a_registry = None

            mock_manager = MagicMock()
            mock_manager.start_heartbeat_loop = AsyncMock()
            sm.pipeline_manager = mock_manager

            with patch("app.service_main.web.AppRunner") as MockRunner:
                mock_runner = AsyncMock()
                MockRunner.return_value = mock_runner

                with patch("app.service_main.web.TCPSite") as MockSite:
                    mock_site = AsyncMock()
                    MockSite.return_value = mock_site

                    async def cancel_after_start(*args, **kwargs):
                        raise asyncio.CancelledError()

                    with patch("asyncio.sleep", side_effect=cancel_after_start):
                        try:
                            await sm.run_server(8080)
                        except asyncio.CancelledError:
                            pass

            # Should complete without errors - no registry calls
        finally:
            sm._a2a_registry = original_registry
            sm.pipeline_manager = original_manager


# ============================================================
# Tests: _get_enable_capability_registry helper
# ============================================================


class TestGetEnableCapabilityRegistry:
    """Tests for the _get_enable_capability_registry() config helper."""

    @patch("app.pipeline_ecs._get_config")
    def test_returns_true_when_enabled(self, mock_config):
        from app.pipeline_ecs import _get_enable_capability_registry

        config = MagicMock()
        config.features.enable_capability_registry = True
        mock_config.return_value = config

        assert _get_enable_capability_registry() is True

    @patch("app.pipeline_ecs._get_config")
    def test_returns_false_when_disabled(self, mock_config):
        from app.pipeline_ecs import _get_enable_capability_registry

        config = MagicMock()
        config.features.enable_capability_registry = False
        mock_config.return_value = config

        assert _get_enable_capability_registry() is False

    @patch("app.pipeline_ecs._get_config", return_value=None)
    def test_returns_false_on_config_error(self, mock_config):
        from app.pipeline_ecs import _get_enable_capability_registry

        assert _get_enable_capability_registry() is False
