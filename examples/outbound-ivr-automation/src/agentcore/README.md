# Outbound IVR on Bedrock AgentCore Runtime

`src/agentcore/livekit_app.py` is the deployed entrypoint: the same Strands `BidiAgent` as
`outbound_ivr.voice.agent` (navigate/intake phases, Nova 2.5 Sonic, all the
pacing/keepalive/closing-watcher/audit behaviour), run over a self-hosted LiveKit room
instead of a media WebSocket. See `docs/design.md`'s "Agent behaviour" section for the
agent logic itself, and `src/agentcore/livekit_app.py`'s own module docstring for the
Runtime invocation contract and how to run it locally against a real LiveKit server.

## Build the container image

```
docker build --platform linux/arm64 -f src/agentcore/Dockerfile -t outbound-ivr-agent-livekit .
```

`infrastructure/scripts/deploy.sh` builds and pushes this as a CDK asset
(`infrastructure/stacks/agent_runtime_stack.py`); you shouldn't normally need to run
this by hand. AgentCore Runtime requires `linux/arm64`, Python 3.12.

## Environment

Set by the CDK stack that deploys this Runtime, not baked into the image. See the root
README's "Deploy" section and `infrastructure/stacks/agent_runtime_stack.py`
for what each one resolves to. `OUTBOUND_IVR_LEVELS=0` is the one baked-in default (no local
dashboard in AWS to feed voice levels to).
