# Outbound IVR on Bedrock AgentCore Runtime, over a self-hosted LiveKit room instead of the
# media WebSocket protocol: the same Strands BidiAgent (outbound_ivr.voice.agent), behind the
# background-task entrypoint in agentcore/livekit_app.py.
#
# AgentCore Runtime requires linux/arm64, so build cross-platform even from an x86 or
# Apple Silicon dev machine. Built with finch, not docker (CDK_DOCKER=finch; see the
# root AGENTS.md's Container Builds convention and infrastructure/scripts/deploy.sh),
# from the example root as the build context:
#   finch build --platform linux/arm64 -f src/agentcore/Dockerfile -t outbound-ivr-agent-livekit .
# infrastructure/scripts/deploy.sh builds and pushes it as a CDK asset (agent_runtime_stack.py).
#
# Base images come from the ECR Public mirror of the Docker official images, and uv is
# pinned to the version that wrote uv.lock, so a rebuild resolves the same toolchain.

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim AS build

RUN pip install --no-cache-dir uv==0.13.6

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/outbound_ivr ./src/outbound_ivr
COPY src/agentcore ./agentcore

# strands: the BidiAgent and Nova Sonic model; agentcore: the bedrock-agentcore SDK's
# app/entrypoint; livekit: the room/SIP client. Confirmed (pyproject.toml's own
# comments) that all three resolve together with no version conflicts.
RUN uv sync --frozen --no-dev --extra strands --extra agentcore --extra livekit

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH"

# No local dashboard in AWS to feed voice levels to; events go to DynamoDB
# (OUTBOUND_IVR_EVENTS_TABLE) and stdout (CloudWatch Logs), the case record and recording to
# S3 (OUTBOUND_IVR_ARTIFACTS_BUCKET) -- see outbound_ivr.voice.agent. The rest of the environment
# (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET_ARN, OUTBOUND_IVR_EVENTS_TABLE,
# OUTBOUND_IVR_ARTIFACTS_BUCKET, OUTBOUND_IVR_OFFICE_NUMBER, OUTBOUND_IVR_TRUNK_NAME, OUTBOUND_IVR_TEAM_DIALER,
# OUTBOUND_IVR_EVIDENCE_NUMBER, OUTBOUND_IVR_HANDOFF_TABLE, OUTBOUND_IVR_SONIC_MODEL, AWS_REGION, ...) is
# set by the CDK stack that deploys this Runtime, not baked into the image.
ENV OUTBOUND_IVR_LEVELS=0
ENV OUTBOUND_IVR_TOOLS=local

# Run as an unprivileged user. Everything the Runtime writes lives under /app/runs
# (event and levels logs, the call recording before upload, the local case-record
# fallback), so only that directory is handed to the user; the code and the virtual
# environment stay root-owned and read-only. HOME points at a writable place for any
# library that caches under ~, and bytecode caching is off because /app is read-only.
RUN useradd --system --uid 10001 --create-home --home-dir /home/outbound_ivr --shell /usr/sbin/nologin outbound_ivr \
    && mkdir -p /app/runs \
    && chown -R outbound_ivr:outbound_ivr /app/runs
ENV HOME=/home/outbound_ivr
ENV PYTHONDONTWRITEBYTECODE=1
USER outbound_ivr

EXPOSE 8080
# opentelemetry-instrument (from the agentcore extra's aws-opentelemetry-distro, on
# PATH via /app/.venv/bin above) auto-instruments the process before app code runs, so
# ADOT's exporter/tracer-provider setup exists for outbound_ivr.voice.agent to hand to
# StrandsTelemetry. Do not set OTEL_*/AGENT_OBSERVABILITY_ENABLED here -- AgentCore
# Runtime injects those itself.
CMD ["opentelemetry-instrument", "python", "agentcore/livekit_app.py"]
