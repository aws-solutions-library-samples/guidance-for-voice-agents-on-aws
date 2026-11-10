Examples live in `examples/`; follow [examples/AGENTS.md](examples/AGENTS.md).

## Documentation Conventions

- Use Mermaid diagrams instead of ASCII art for all architecture diagrams, flow charts, and sequence diagrams in Markdown files.

## Logging Conventions

All Python code in this project uses **structlog** for structured logging. Do not use stdlib `logging` or `loguru`.

**Setup pattern:**
```python
import structlog

logger = structlog.get_logger(__name__)
```

**Log call style** — use snake_case event names as the first argument, then keyword arguments for structured data. Never use printf-style `%s` formatting or f-strings in log messages:
```python
# Correct
logger.info("tool_execution_complete", tool_name=name, execution_time_ms=42.1, call_id=cid)
logger.warning("task_protection_enable_failed", note="Accepting call without protection")
logger.error("pipeline_error", error=str(e), error_type=type(e).__name__)

# Wrong — do not use these patterns
logger.info("Tool %s completed in %.1fms", name, elapsed)   # printf style
logger.info(f"Tool {name} completed in {elapsed:.1f}ms")     # f-string style
```

**For capability agents** (separate containers): add `structlog` to `requirements.txt` and configure it at the top of the entrypoint:
```python
import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)
```

The voice agent's `service_main.py` already configures structlog globally with `contextvars` support for per-call context binding.

## Container Builds

Use `finch` instead of Docker for all container image builds and deployments. For CDK deployments, set the `CDK_DOCKER` environment variable to `finch` so that CDK asset bundling uses finch:

```bash
export CDK_DOCKER=finch
```

## AWS Configuration

Configure your AWS CLI with credentials for the target account before deploying:

```bash
aws configure --profile <your-profile>
export AWS_PROFILE=<your-profile>
```

## Environment Variables

### Observability Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENVIRONMENT` | `production` | Deployment environment (poc, dev, staging, prod) - used as CloudWatch dimension |
| `ENABLE_AUDIO_QUALITY_MONITORING` | `true` | Enable AudioQualityObserver for RMS/peak/silence metrics |
| `ENABLE_CONVERSATION_LOGGING` | `false` | Enable ConversationObserver for transcript logging |
| `ENABLE_TOOL_CALLING` | `true` | Enable LLM tool calling (function calling) for executing actions |
| `ENABLE_FILLER_PHRASES` | `true` | Enable filler phrases during tool execution delays |
| `FILLER_DELAY_THRESHOLD_MS` | `1500` | Milliseconds to wait before playing filler phrase |

### LLM Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MODEL_ID` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Bedrock model/inference profile ID for LLM processing. Also configurable via SSM at `/voice-agent/config/llm-model-id` |

### Capability Agent Registry (A2A) Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `A2A_NAMESPACE` | *required* | CloudMap HTTP namespace name for agent discovery - set by CDK |
| `A2A_CACHE_TTL_SECONDS` | `60` | Response cache TTL for A2A tool results |
| `A2A_CACHE_MAX_SIZE` | `100` | Max cached A2A responses per tool handler |

Feature flag via SSM at `/voice-agent/config/enable-capability-registry` (default: `false`). When enabled, the voice agent discovers capability agents (KB, CRM) via CloudMap and registers their skills as LLM tools via the A2A protocol.

For a step-by-step guide on adding a new capability agent, see [Adding a Capability Agent](docs/guides/adding-a-capability-agent.md). For architecture details, see [Capability Agent Pattern](docs/patterns/capability-agent-pattern.md).

Additional SSM config:
- `/voice-agent/a2a/poll-interval-seconds` (default: `30`) - CloudMap polling interval
- `/voice-agent/a2a/tool-timeout-seconds` (default: `30`) - A2A call timeout

### Knowledge Base Agent Configuration

These env vars are set on the **KB capability agent** container (not the voice agent):

| Variable | Default | Description |
|----------|---------|-------------|
| `KB_KNOWLEDGE_BASE_ID` | *required* | Bedrock Knowledge Base ID - set by CDK deployment |
| `KB_RETRIEVAL_MAX_RESULTS` | `3` | Maximum document chunks to retrieve per query (1-10) |
| `KB_MIN_CONFIDENCE_SCORE` | `0.3` | Minimum confidence threshold for results (0.0-1.0) |

The KB agent uses a `DirectToolExecutor` that bypasses the inner Strands LLM, calling `search_knowledge_base` directly. This reduces A2A tool latency from ~2,742ms to ~323ms.

### Auto-Scaling Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_CONCURRENT_CALLS` | `10` | Maximum concurrent calls per container before `/ready` returns 503 and NLB stops routing new calls. Driven by `sessionCapacityPerTask` in CDK config. |

Scaling behavior is configured via CDK context or environment variables at deploy time:

| Parameter | Default | Env Var | Description |
|-----------|---------|---------|-------------|
| `minCapacity` | `1` | `MIN_CAPACITY` | Minimum ECS tasks (always running) |
| `maxCapacity` | `12` | `MAX_CAPACITY` | Maximum ECS tasks |
| `targetSessionsPerTask` | `3` | `TARGET_SESSIONS_PER_TASK` | Target tracking metric target (validated 1-10) |
| `sessionCapacityPerTask` | `10` | `SESSION_CAPACITY_PER_TASK` | Per-task session capacity: `/ready` returns 503 at this limit (validated 1-50, must be >= targetSessionsPerTask). Also sets `MAX_CONCURRENT_CALLS` container env var and drives dashboard annotations + alarm thresholds. |

Example: `npx cdk deploy -c voice-agent:minCapacity=2 -c voice-agent:maxCapacity=20 -c voice-agent:sessionCapacityPerTask=8`

### Cold Start Timing (Measured)

New ECS tasks take **~90s** from creation to receiving traffic. The scaling decision pipeline adds 1-3 min on top. Total time from overload to new capacity serving calls: **~3-5 min**.

| Phase | Duration | Cumulative |
|-------|----------|-----------|
| ENI attach + scheduling | ~14s | 14s |
| Image pull (824 MB compressed) | ~37s | 51s |
| Container init | ~17s | 68s |
| NLB health check (2 x 10s) | ~20s | ~88s |

The image pull is the bottleneck and cannot be reduced significantly -- the container requires ffmpeg, libsndfile, pipecat, and ONNX Runtime. Scaling test scenarios must include a 5-minute wait between triggering scale-out and sending overflow calls.

### Transfer Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSFER_DESTINATION` | *none* | SIP URI for call transfers (e.g., `sip:user-b@192.168.1.100:5060`) |

When `TRANSFER_DESTINATION` is set and `ENABLE_TOOL_CALLING` is true, the voice agent can transfer calls to human agents via SIP REFER. The capability-based tool system automatically detects this variable and only registers the `transfer_to_agent` tool when it is set.

### Tool Capability System

Local tools declare which pipeline capabilities they require via a `requires` field on `ToolDefinition`. At pipeline creation time, `detect_capabilities()` probes the runtime environment and only registers tools whose requirements are fully satisfied.

**Pipeline Capabilities:**

| Capability | Detected When | Used By |
|------------|--------------|---------|
| `BASIC` | Always available | `get_current_time` |
| `TRANSPORT` | `DailyTransport` is present | Hangup, DTMF collection (future) |
| `SIP_SESSION` | Pipeline has a SIP dial-in tracker | `transfer_to_agent` |
| `DTMF_COLLECTION` | Transport supports DTMF input | PIN/payment collection (future) |
| `RECORDING_CONTROL` | Transport supports pause/resume | PCI-DSS payment flow (future) |
| `TRANSFER_DESTINATION` | `TRANSFER_DESTINATION` env var is set | `transfer_to_agent` |

**Tool Catalog:** All local tools are listed in `app/tools/builtin/catalog.py`. New tools only need to be added there with appropriate `requires` -- no pipeline code changes needed.

**Disabled Tools Override:** SSM parameter `/voice-agent/config/disabled-tools` (comma-separated tool names) allows explicitly disabling tools even when their capabilities are satisfied. Example: `transfer_to_agent,hangup_call`.

### CloudWatch Monitoring

The voice agent emits metrics to the `VoiceAgent/Pipeline` namespace. Key metrics:

- **E2ELatency**: End-to-end latency from user speech to TTS audio (ms)
- **TurnCount**: Number of conversation turns per call
- **InterruptionCount**: Barge-in events per call
- **AudioRMS/AudioPeak**: Audio quality levels (dBFS)
- **PoorAudioTurns**: Turns with audio below -55 dBFS threshold
- **ActiveSessions**: Current concurrent session count
- **ToolExecutionTime**: Tool execution duration (ms) - when tool calling enabled
- **ToolInvocationCount**: Number of tool invocations - when tool calling enabled

### CloudWatch Alarms

| Alarm | Threshold | Action |
|-------|-----------|--------|
| E2E Latency High | Avg > 2000ms for 3 periods | Check LLM/TTS latency breakdown |
| Error Rate High | > 5% over 5 minutes | Check error category in logs |
| CPU Utilization High | > 80% for 2/3 periods | Consider scaling up |
| Memory Utilization High | > 85% for 2/3 periods | Check for memory leaks |
| Container Restarts | > 2/hour | Check container logs for crash cause |
| Sessions Per Task High | Avg > 4.0 for 2 consecutive periods | Approaching per-container capacity; check if scaling policies are responding |
| Task Protection Failure | >= 1 `task_protection_all_retries_exhausted` in 5 min | Task protection API failed; active calls may not be safe from scale-in |
| Metric Staleness | `SessionsPerTask` INSUFFICIENT_DATA for 5 periods | Session counter Lambda stopped emitting; auto-scaling is blind |

Dashboard URL is output as `VoiceAgentEcs.DashboardUrl` in CloudFormation outputs.
