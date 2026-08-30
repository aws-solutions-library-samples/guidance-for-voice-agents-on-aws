# Publishing a SageMaker Model

This guide is for **model providers** — companies building STT or TTS models who want to
publish on AWS Marketplace.

If you want to deploy a custom model, see [Build real-time voice applications with Amazon
SageMaker AI and vLLM](https://aws.amazon.com/blogs/machine-learning/build-real-time-voice-applications-with-amazon-sagemaker-ai-and-vllm/).

## 1. Make your container SageMaker Marketplace-compliant

Your model needs to implement the SageMaker Marketplace container contract (`/ping` +
`/invocations`, optional bidirectional WebSocket, optional per-inference metering, local
testing, ECR push, `CreateModelPackage`). The `sagemaker-marketplace-onboarding` skill walks
through this end to end:

```
/plugin marketplace add aws-samples/sample-apj-sup-sa
/plugin install sagemaker-marketplace-onboarding@apj-sup-sa
```

Or browse it directly: [`ai-infra/sagemaker-marketplace-onboarding`](https://github.com/aws-samples/sample-apj-sup-sa/tree/main/ai-infra/sagemaker-marketplace-onboarding)
in `aws-samples/sample-apj-sup-sa`.

## 2. Make it Pipecat-compatible

This Guidance orchestrates STT/TTS models with [Pipecat](https://github.com/pipecat-ai/pipecat).
For your model to be consumable the way Deepgram's is here, your container's WebSocket protocol
needs a control-message vocabulary an orchestrator can drive (start/flush/cancel/keepalive, etc.).
See the same skill's
[`reference/pipecat-integration.md`](https://github.com/aws-samples/sample-apj-sup-sa/blob/main/ai-infra/sagemaker-marketplace-onboarding/reference/pipecat-integration.md)
for the architecture, the control-message table, and both Pipecat contribution paths
(community-maintained integration vs. a PR into Pipecat core).

## 3. Wire it into this Guidance

Once your model is deployed on a SageMaker endpoint, this repo picks it up through
`backend/voice-agent/app/services/factory.py`'s provider switch — the same mechanism that
selects between Deepgram's cloud API and its SageMaker endpoints today:

- `STT_PROVIDER=sagemaker` (or `flux-sagemaker`) and `TTS_PROVIDER=sagemaker` select the
  SageMaker-hosted path over the default cloud APIs.
- Each provider branch reads its endpoint name from config (`STT_ENDPOINT_NAME` /
  `TTS_ENDPOINT_NAME` or the Flux-specific equivalent) and the AWS region, then constructs the
  Pipecat service class from step 2 with those values.
- `patch_sagemaker_bidi_credentials()` is called before constructing the service — needed for
  the bidi client's credential resolution in this deployment's environment.

**Gotcha to watch for:** the STT service's `sample_rate` must match the transport's actual
sample rate (16000 for local WebRTC, 8000 for PSTN/telephony in this repo). A mismatch doesn't
raise an error — the BiDi handshake just hangs silently, since Pipecat's built-in STT service
has no timeout on this. Check `STT_SAMPLE_RATE`/transport config carefully when wiring in a new
provider.

Adding a new named provider option (rather than pointing the existing `sagemaker` value at a
different endpoint) means adding a new branch in `factory.py` alongside the existing ones — see
the `flux-sagemaker` branch as the template for what a new provider option looks like structurally.
