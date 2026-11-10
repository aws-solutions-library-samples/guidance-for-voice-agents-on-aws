"""Patch Pipecat's SageMakerBidiClient to support ECS Fargate credentials.

The built-in client only uses EnvironmentCredentialsResolver, which requires
AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env vars. On ECS Fargate, credentials
come from the task role via the container metadata endpoint. This patch adds
ContainerCredentialsResolver to the credential chain so BiDi streaming works
in both local dev and ECS environments.
"""

import os

import structlog

logger = structlog.get_logger(__name__)

_patched = False


def patch_sagemaker_bidi_credentials():
    """Monkey-patch SageMakerBidiClient._initialize_client for ECS support.

    Must be called before any SageMakerBidiClient instances are created.
    Safe to call multiple times — only patches once.
    """
    global _patched
    if _patched:
        return
    _patched = True

    from aws_sdk_sagemaker_runtime_http2.client import AsyncSageMakerRuntimeHTTP2Client
    from aws_sdk_sagemaker_runtime_http2.config import Config, HTTPAuthSchemeResolver
    from smithy_aws_core.auth.sigv4 import SigV4AuthScheme
    from smithy_aws_core.identity import EnvironmentCredentialsResolver
    from smithy_aws_core.identity.container import ContainerCredentialsResolver
    from smithy_core.aio.identity import ChainedIdentityResolver
    from smithy_http.aio.aiohttp import AIOHTTPClient

    from pipecat.services.aws.sagemaker.bidi_client import SageMakerBidiClient

    def _patched_initialize_client(self):
        """Initialize SageMaker BiDi client with ECS-compatible credential chain.

        Credential resolution order:
        1. EnvironmentCredentialsResolver — AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY
        2. ContainerCredentialsResolver — ECS task role via metadata endpoint
        """
        logger.debug("sagemaker_bidi_client_initializing", region=self.region)
        logger.debug("sagemaker_bidi_endpoint", endpoint_uri=self.bidi_endpoint)

        # Build credential resolver chain
        resolvers = [EnvironmentCredentialsResolver()]

        # Add ContainerCredentialsResolver when running on ECS/EKS
        container_creds_uri = os.getenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
        container_creds_full_uri = os.getenv("AWS_CONTAINER_CREDENTIALS_FULL_URI")

        if container_creds_uri or container_creds_full_uri:
            logger.info(
                "ecs_container_credentials_detected",
                action="adding ContainerCredentialsResolver to credential chain",
            )
            http_client = AIOHTTPClient()
            resolvers.append(ContainerCredentialsResolver(http_client=http_client))
        else:
            logger.debug("no_ecs_container_credentials_env_vars")

        credential_resolver = ChainedIdentityResolver(resolvers=resolvers)

        config = Config(
            endpoint_uri=self.bidi_endpoint,
            region=self.region,
            aws_credentials_identity_resolver=credential_resolver,
            auth_scheme_resolver=HTTPAuthSchemeResolver(),
            auth_schemes={"aws.auth#sigv4": SigV4AuthScheme(service="sagemaker")},
        )
        self._client = AsyncSageMakerRuntimeHTTP2Client(config=config)
        logger.debug("sagemaker_bidi_client_initialized", resolver="chained_credential")

    SageMakerBidiClient._initialize_client = _patched_initialize_client
    logger.info(
        "sagemaker_bidi_client_patched", resolver="ecs_compatible_credential_chain"
    )
