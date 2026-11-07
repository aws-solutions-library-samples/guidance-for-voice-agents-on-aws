"""outbound-ivr-dashboard: the deployed dashboard SPA, its API, and its own
Cognito user pool.

See `../src/lambdas/api/README.md` for the routes, the per-route IAM, and the
`outbound-ivr-events` item shape this depends on, and docs/design.md for the
dashboard's place in the overall design.

- **Site:** a private S3 bucket, read only through CloudFront via Origin
  Access Control (never directly, never through a bucket policy allowing
  the public) -- `dashboard/dist`, built with `VITE_BACKEND=aws`
  (`npm run build:aws`), plus a `config.json` this stack writes alongside
  it at deploy time (not baked into the build, so the same SPA build
  works against any environment).
- **API:** one HTTP API, one Lambda, a Cognito JWT authorizer on every
  route (the API's `default_authorizer`, so there's no route that can
  accidentally skip it).
- **Users:** a *separate* Cognito user pool from the AgentCore stack's
  M2M-only pool -- this one has actual human users (admin-created, no
  self sign-up), with the Hosted UI and an authorization-code-with-PKCE
  app client for the SPA.

The resource-building logic lives in `DashboardConstruct`, a plain holder
(not a `Construct` subclass -- same "build directly on the given scope"
convention `data_stack.py`'s `DataConstruct` uses) so `dashboard_stack.py`'s
top-level `DashboardStack` and its `DashboardNestedStack` (the one
`outbound_ivr_stack.py` actually nests inside `OutboundIvrStack`) can both
reuse it without duplicating this file.

`monitor_bucket` (`None` by default) wires the `OUTBOUND_IVR_MONITOR_BUCKET` env var
and the IAM the `GET /api/calls/{call}/monitor` route needs to poll it --
the route itself is always added, but `lambdas/api/handler.py`'s handler
404s whenever `OUTBOUND_IVR_MONITOR_BUCKET` is unset, which is always true for this
stack's own caller below. Unlike the previous ticket-mint design, the
monitor IAM is granted directly here, inside `DashboardConstruct` (the API
Lambda role lives in this same nested stack as the bucket reference it is
given -- no cross-nested-stack grant to make at a composition root).
`ccp_url`/`connect_region` land in `config.json` unconditionally (defaulting
to `""`/`REGION`), for the dashboard's embedded CCP.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
)
from aws_cdk import (
    aws_apigatewayv2_authorizers as apigwv2_authorizers,
)
from aws_cdk import (
    aws_apigatewayv2_integrations as apigwv2_integrations,
)
from aws_cdk import (
    aws_cloudfront as cloudfront,
)
from aws_cdk import (
    aws_cloudfront_origins as origins,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_lambda as _lambda,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3_deployment as s3deploy,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from .constants import REGION

# Shared with gateway_stack.py's own tools Lambda bundling (see that file's
# `_build_tools_lambda_asset`), so both bundle identically.
from .lambda_bundling import build_outbound_ivr_tools_lambda_asset


def build_runtime_config(
    *,
    api_url: str,
    cognito_domain: str,
    cognito_client_id: str,
    cognito_region: str,
    region: str,
    ccp_url: str,
    connect_region: str,
) -> dict:
    """The `config.json` dict written alongside the built SPA (see
    `dashboard/src/lib/config.ts`'s `RuntimeConfig`) -- a plain function, not
    inlined in `DashboardConstruct`, so a test can assert on its shape directly
    instead of reaching into a `BucketDeployment`'s bundled asset content.

    No `monitorWsUrl` field any more: the Monitor feed is polled through
    `apiUrl` like every other dashboard call, not a separate WebSocket
    origin."""
    return {
        "apiUrl": api_url,
        "cognitoDomain": cognito_domain,
        "cognitoClientId": cognito_client_id,
        "cognitoRegion": cognito_region,
        "region": region,
        # The embedded CCP: defaults empty, so infra's own DashboardStack
        # below (which passes no ccp_url) is unchanged -- the SPA treats an
        # empty string as "not configured".
        "ccpUrl": ccp_url,
        "connectRegion": connect_region,
    }


def content_security_policy(
    *,
    region: str,
    cognito_region: str,
    cognito_domain: str,
    ccp_url: str,
) -> str:
    """The dashboard's CSP.

    The managed `ResponseHeadersPolicy.SECURITY_HEADERS` this distribution used
    to rely on sets HSTS, X-Content-Type-Options, X-Frame-Options, a referrer
    policy and X-XSS-Protection, and no CSP at all. That matters more here than
    on a typical SPA: the operator's refresh token lives in `localStorage` for 30
    days, and the app can start real phone calls, so one injected script is worth
    a month of access.

    Where this is deliberately loose:
    - `style-src 'unsafe-inline'`, because the bundler emits inline styles.
    - the API host is a wildcard. The HTTP API's domain is generated and the
      distribution has to exist before the API (the API's CORS config names this
      distribution), so the exact host is not knowable here. Narrowed to API
      Gateway in one region.
    - `blob:` in `worker-src`/`media-src`: the Monitor's Web Audio playback path
      (`AudioContext`/`AudioBufferSourceNode`, not LiveKit) needs it for the same
      directive. No separate WebSocket origin any more -- the Monitor feed is
      polled through the API host above, like every other dashboard call.
    """
    api_host = f"https://*.execute-api.{region}.amazonaws.com"
    connect_src = [
        "'self'",
        api_host,
        f"https://cognito-idp.{cognito_region}.amazonaws.com",
        # The Hosted UI host, which is NOT the same as cognito-idp: auth.ts points
        # token_endpoint, userinfo_endpoint and revocation_endpoint at it, so
        # completeSignIn() cannot exchange the redirect for tokens without it.
        # Omitting it would have locked out every login the moment anyone followed
        # the documented next step and passed -c enforceCsp=true.
        f"https://{cognito_domain}",
    ]
    frame_src = [ccp_url] if ccp_url else ["'none'"]
    directives = [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        f"connect-src {' '.join(connect_src)}",
        f"frame-src {' '.join(frame_src)}",
        "worker-src 'self' blob:",
        "media-src 'self' blob: mediastream:",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    ]
    return "; ".join(directives)


class DashboardConstruct:
    """Builds the dashboard's site/CloudFront/Cognito/API/Lambda directly on
    `scope` (see this module's docstring for why: identical logical IDs to
    building them straight in a `Stack`, so `infra`'s own synth output is
    unaffected by this extraction).

    `calls_queue=None` drops the SQS grant/env var entirely (the CDK app's
    LiveKit/AgentCore path calls `InvokeAgentRuntime` directly, no queue to
    drain). `agent_runtime_arn`, when given, adds the
    `bedrock-agentcore:InvokeAgentRuntime` grant (scoped to that ARN and its
    `runtime-endpoint/*` children) and the `OUTBOUND_IVR_RUNTIME_ARN` env var the
    Lambda needs to start a call.
    """

    def __init__(
        self,
        scope: Construct,
        *,
        cases_table: dynamodb.Table,
        offices_table: dynamodb.Table,
        events_table: dynamodb.Table,
        artifacts_bucket: s3.Bucket,
        calls_queue: sqs.Queue | None,
        data_kms_key: kms.Key,
        bucket_name: str,
        http_api_name: str,
        users_pool_name: str,
        users_domain_prefix: str,
        spa_client_name: str,
        api_function_name: str,
        agent_runtime_arn: str | None = None,
        monitor_bucket: s3.Bucket | None = None,
        ccp_url: str = "",
        connect_region: str = REGION,
    ) -> None:
        dashboard_dist_dir = (
            scope.node.try_get_context("dashboardDistDir") or "../src/dashboard/dist"
        )
        api_lambda_dir = scope.node.try_get_context("apiLambdaDir") or "../src/lambdas/api"
        # Context key "outboundIvrPackageDir" is a shared contract with
        # dashboard_stack.py, which sets context under this exact key (its own
        # _CONTEXT_DEFAULTS) so this construct picks up its app-relative path
        # instead of the default below.
        outbound_ivr_package_dir = (
            scope.node.try_get_context("outboundIvrPackageDir") or "../src/outbound_ivr"
        )
        dev_callback_url = (
            scope.node.try_get_context("devCallbackUrl") or "http://localhost:5199/auth/callback"
        )

        # ------------------------------------------------------------------
        # Site: a private bucket, CloudFront with Origin Access Control.
        # ------------------------------------------------------------------
        site_bucket = s3.Bucket(
            scope,
            "SiteBucket",
            bucket_name=bucket_name,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        NagSuppressions.add_resource_suppressions(
            site_bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": (
                        "Demo bucket serving a built SPA with no sensitive "
                        "content; access logging adds cost and a second "
                        "bucket with no consumer here."
                    ),
                }
            ],
        )

        # The managed SECURITY_HEADERS policy carries no CSP, so this one adds one
        # while keeping everything that policy set (same HSTS, nosniff, referrer
        # policy, and frame-deny).
        #
        # Report-only by default. An enforced CSP that is slightly wrong does not
        # degrade the dashboard, it breaks it: a blocked worker takes out the
        # monitor's audio, a blocked frame takes out the softphone, and the
        # failure shows up as a blank panel in front of whoever is watching the
        # demo. Report-only puts the violations in the browser console instead.
        # Click through the dashboard once, including listen-in and the
        # softphone, then deploy with `-c enforceCsp=true`.
        csp = content_security_policy(
            region=REGION,
            # The user pool is created in this stack, so its region is this one.
            cognito_region=REGION,
            # Same expression as build_runtime_config's own cognito_domain below,
            # deliberately: if the two drift, sign-in breaks under an enforced CSP.
            cognito_domain=f"{users_domain_prefix}.auth.{REGION}.amazoncognito.com",
            ccp_url=ccp_url,
        )
        enforce_csp = str(scope.node.try_get_context("enforceCsp") or "").lower() == "true"
        security_headers = cloudfront.ResponseHeadersPolicy(
            scope,
            "SiteSecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=(
                    cloudfront.ResponseHeadersContentSecurityPolicy(
                        content_security_policy=csp, override=True
                    )
                    if enforce_csp
                    else None
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(730),
                    include_subdomains=True,
                    override=True,
                ),
                xss_protection=cloudfront.ResponseHeadersXSSProtection(
                    protection=True, mode_block=True, override=True
                ),
            ),
            custom_headers_behavior=(
                None
                if enforce_csp
                else cloudfront.ResponseCustomHeadersBehavior(
                    custom_headers=[
                        cloudfront.ResponseCustomHeader(
                            header="Content-Security-Policy-Report-Only",
                            value=csp,
                            override=True,
                        )
                    ]
                )
            ),
        )

        distribution = cloudfront.Distribution(
            scope,
            "Distribution",
            # SECURITY EXCEPTION (accepted_with_reason): public by design --
            # a browser-based SPA has to be fetchable with no credentials
            # before it can even show a login screen. What restricts it:
            # the SPA itself carries no sensitive data (static built
            # assets only), and the HTTP API it calls for every real piece
            # of data requires a Cognito JWT on every single route (see
            # HttpApi's own description below) -- so reaching this
            # distribution is not the same as reaching any case data.
            comment=(
                "outbound-ivr dashboard: public SPA host, no sensitive content; "
                "the API it calls requires a Cognito JWT on every route"
            ),
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=security_headers,
            ),
            default_root_object="index.html",
            # SPA fallback: client-side routes (e.g. /cases/<id>, /auth/callback)
            # have no matching S3 object, so S3 (via CloudFront) returns
            # 403/404 for them; both get rewritten to index.html so the SPA's
            # own router handles the path.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403, response_http_status=200, response_page_path="/index.html"
                ),
                cloudfront.ErrorResponse(
                    http_status=404, response_http_status=200, response_page_path="/index.html"
                ),
            ],
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )
        NagSuppressions.add_resource_suppressions(
            distribution,
            [
                {
                    "id": "AwsSolutions-CFR1",
                    "reason": "Demo, no geographic restriction requirement.",
                },
                {
                    "id": "AwsSolutions-CFR2",
                    "reason": (
                        "No AWS WAF web ACL for a demo distribution behind "
                        "a Cognito-authenticated API; the SPA itself is "
                        "static content with nothing to exploit without "
                        "valid credentials against the API."
                    ),
                },
                {
                    "id": "AwsSolutions-CFR3",
                    "reason": "No access logging: demo distribution, no log consumer.",
                },
                {
                    "id": "AwsSolutions-CFR4",
                    "reason": (
                        "No custom domain/ACM certificate for this demo, "
                        "so this distribution uses CloudFront's default "
                        "*.cloudfront.net viewer certificate; this rule "
                        "flags that case specifically because "
                        "'minimum_protocol_version' is only enforced for "
                        "distributions with their own viewer certificate "
                        "-- the default certificate's own security policy "
                        "already requires at least TLS 1.2."
                    ),
                },
            ],
        )
        cloudfront_domain = f"https://{distribution.distribution_domain_name}"

        # ------------------------------------------------------------------
        # Users: a separate Cognito user pool from AgentCoreStack's M2M-only
        # pool -- this one has real human users, admin-created only (no
        # self sign-up), optional TOTP MFA, and a Hosted UI domain.
        # ------------------------------------------------------------------
        users_pool = cognito.UserPool(
            scope,
            "DashboardUsersPool",
            user_pool_name=users_pool_name,
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            # REQUIRED, not OPTIONAL: this pool gates human access to case
            # data behind the public CloudFront site + HTTP API (see
            # accepted_with_reason), so every admin-created operator must
            # enroll TOTP MFA, not merely be offered it. cdk-nag's
            # AwsSolutions-COG2 flags anything less for exactly this reason.
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            # Real human accounts (admin-created, no self sign-up), unlike
            # AgentCoreStack's M2M-only pool -- so, unlike that one, a real
            # password policy applies here.
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        NagSuppressions.add_resource_suppressions(
            users_pool,
            [
                {
                    "id": "AwsSolutions-COG8",
                    "reason": (
                        "No AdvancedSecurityMode (requires the Plus "
                        "feature plan, a paid tier) for a demo pool with "
                        "a handful of admin-created operator accounts."
                    ),
                },
            ],
        )
        users_domain = users_pool.add_domain(
            "DashboardUsersDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=users_domain_prefix),
        )
        # generate_secret=False: a public SPA client. Cognito requires PKCE
        # for the authorization-code grant on public clients automatically
        # -- there is no separate "require PKCE" flag to set.
        dashboard_client = users_pool.add_client(
            "DashboardSpaClient",
            user_pool_client_name=spa_client_name,
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[f"{cloudfront_domain}/auth/callback", dev_callback_url],
                logout_urls=[cloudfront_domain, dev_callback_url.rsplit("/auth/callback", 1)[0]],
            ),
            prevent_user_existence_errors=True,
        )

        # ------------------------------------------------------------------
        # API Lambda: the same handler.lambda_handler + copied-in
        # outbound_ivr.tools package approach as the Gateway's tools Lambda (see
        # lambdas/api/README.md "Packaging").
        # ------------------------------------------------------------------
        api_lambda_role = iam.Role(
            scope,
            "ApiLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        api_lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadCases",
                actions=["dynamodb:Scan", "dynamodb:GetItem"],
                resources=[cases_table.table_arn],
            )
        )
        api_lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadOffices",
                actions=["dynamodb:GetItem"],
                resources=[offices_table.table_arn],
            )
        )
        api_lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadEvents",
                actions=["dynamodb:Query"],
                resources=[events_table.table_arn],
            )
        )
        api_lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="UpdateCaseStatus",
                actions=["dynamodb:UpdateItem"],
                resources=[cases_table.table_arn],
            )
        )
        api_lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadCaseRecords",
                actions=["s3:GetObject"],
                resources=[artifacts_bucket.arn_for_objects("cases/*")],
            )
        )
        api_lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="ListCaseRecords",
                actions=["s3:ListBucket"],
                resources=[artifacts_bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": ["cases/*"]}},
            )
        )
        api_lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="PresignRecordings",
                actions=["s3:GetObject"],
                resources=[artifacts_bucket.arn_for_objects("recordings/*")],
            )
        )
        if calls_queue is not None:
            api_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    sid="QueueCalls",
                    actions=["sqs:SendMessage"],
                    resources=[calls_queue.queue_arn],
                )
            )
        if agent_runtime_arn is not None:
            # The CDK app's LiveKit/AgentCore path: this Lambda starts a call
            # itself (InvokeAgentRuntime), no SQS dispatcher to drain. Scoped
            # to exactly this Runtime and its runtime-endpoint children, per
            # the shared contract -- never a wildcard.
            api_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeOutboundIvrRuntime",
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[agent_runtime_arn, f"{agent_runtime_arn}/runtime-endpoint/*"],
                )
            )
        if monitor_bucket is not None:
            # GET /api/calls/{call}/monitor: lists and reads segments,
            # reads and (re)writes watch markers. Scoped to this one bucket
            # and its own `seg/`/`watch/` key prefixes, never a wildcard
            # across buckets.
            api_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ListMonitorSegments",
                    actions=["s3:ListBucket"],
                    resources=[monitor_bucket.bucket_arn],
                    conditions={"StringLike": {"s3:prefix": ["*/seg/*"]}},
                )
            )
            api_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadMonitorObjects",
                    actions=["s3:GetObject"],
                    resources=[
                        monitor_bucket.arn_for_objects("*/seg/*"),
                        monitor_bucket.arn_for_objects("*/watch/*"),
                    ],
                )
            )
            api_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    sid="WriteMonitorWatchMarkers",
                    actions=["s3:PutObject"],
                    resources=[monitor_bucket.arn_for_objects("*/watch/*")],
                )
            )
        # One shared CMK across cases/offices/events tables, the artifacts
        # bucket, the monitor bucket, and the calls queue (see DataStack):
        # one grant covers every read and write above instead of
        # hand-rolling kms:Decrypt/Encrypt/GenerateDataKey* per resource.
        data_kms_key.grant_encrypt_decrypt(api_lambda_role)
        NagSuppressions.add_resource_suppressions(
            api_lambda_role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWSLambdaBasicExecutionRole (CloudWatch Logs "
                        "only); no customer-authored equivalent."
                    ),
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "kms:Decrypt/Encrypt/ReEncrypt*/GenerateDataKey* "
                        "via grant_encrypt_decrypt() have no "
                        "resource-level permissions narrower than the "
                        "shared CMK itself. s3:GetObject is scoped to the "
                        "'cases/*' and 'recordings/*' object-key prefixes "
                        "of exactly one bucket, and (when a monitor bucket "
                        "is given) the 'seg/*' and 'watch/*' object-key "
                        "prefixes of exactly one other bucket -- the "
                        "trailing '*' is each object-key-prefix suffix, "
                        "not a cross-resource wildcard, and ListMonitor"
                        "Segments's own s3:ListBucket carries a matching "
                        "s3:prefix condition. Every other action above is "
                        "scoped to one exact table, queue, or (for "
                        "InvokeOutboundIvrRuntime) the Runtime's own ARN plus its "
                        "runtime-endpoint children."
                    ),
                    "appliesTo": [
                        "Action::kms:Decrypt",
                        "Action::kms:Encrypt*",
                        "Action::kms:ReEncrypt*",
                        "Action::kms:GenerateDataKey*",
                        f"Resource::<{Stack.of(scope).get_logical_id(artifacts_bucket.node.default_child)}.Arn>/cases/*",
                        f"Resource::<{Stack.of(scope).get_logical_id(artifacts_bucket.node.default_child)}.Arn>/recordings/*",
                        # A plain f-string with the raw token wouldn't match
                        # cdk-nag's own <LogicalId.Attribute> rendering of a
                        # cross-nested-stack GetAtt reference, hence the
                        # regex rather than trying to reproduce that
                        # rendering by hand -- same reasoning for the
                        # monitor bucket's own seg/watch patterns below.
                        *(
                            [{"regex": "/^Resource::.*\\/runtime-endpoint\\/\\*$/"}]
                            if agent_runtime_arn
                            else []
                        ),
                        *(
                            [
                                {"regex": "/^Resource::.*\\/seg\\/\\*$/"},
                                {"regex": "/^Resource::.*\\/watch\\/\\*$/"},
                            ]
                            if monitor_bucket
                            else []
                        ),
                    ],
                },
            ],
            apply_to_children=True,
        )

        api_lambda_asset_dir = build_outbound_ivr_tools_lambda_asset(
            api_lambda_dir, outbound_ivr_package_dir, include_structlog=True
        )
        environment = {
            "OUTBOUND_IVR_CASES_TABLE": cases_table.table_name,
            "OUTBOUND_IVR_OFFICES_TABLE": offices_table.table_name,
            "OUTBOUND_IVR_EVENTS_TABLE": events_table.table_name,
            "OUTBOUND_IVR_ARTIFACTS_BUCKET": artifacts_bucket.bucket_name,
        }
        if calls_queue is not None:
            environment["OUTBOUND_IVR_CALLS_QUEUE_URL"] = calls_queue.queue_url
        if agent_runtime_arn is not None:
            environment["OUTBOUND_IVR_RUNTIME_ARN"] = agent_runtime_arn
        if monitor_bucket is not None:
            environment["OUTBOUND_IVR_MONITOR_BUCKET"] = monitor_bucket.bucket_name
        api_lambda = _lambda.Function(
            scope,
            "ApiLambda",
            function_name=api_function_name,
            runtime=_lambda.Runtime.PYTHON_3_14,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(api_lambda_asset_dir),
            role=api_lambda_role,
            # Lambda's CPU scales with memory; at the 128 MB default a warm request
            # took ~300 ms. The dashboard polls, so this is the one hot function.
            memory_size=512,
            timeout=Duration.seconds(10),
            # Explicit group rather than `log_retention`, which would add a
            # singleton custom-resource Lambda holding logs:PutRetentionPolicy on
            # "*". The implicit group the Lambda service creates never expires,
            # and this function logs on every dashboard poll, so "never" is a bill
            # as well as a retention question.
            # Deliberately not "/aws/lambda/<function name>": that group already
            # exists in the deployed account, created implicitly by the
            # Lambda service and owned by nothing, so claiming the name fails the
            # update with ResourceAlreadyExistsException. LoggingConfig points the
            # function at this group regardless of what it is called.
            log_group=logs.LogGroup(
                scope,
                "ApiLambdaLogGroup",
                log_group_name=f"/{api_function_name}/lambda",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment=environment,
        )
        self.api_lambda = api_lambda

        # ------------------------------------------------------------------
        # API: one HTTP API, a Cognito JWT authorizer as the *default*
        # authorizer (applies to every route added without an explicit
        # override -- there is no route below that overrides it), CORS
        # limited to the CloudFront domain only.
        # ------------------------------------------------------------------
        jwt_authorizer = apigwv2_authorizers.HttpJwtAuthorizer(
            "DashboardJwtAuthorizer",
            f"https://cognito-idp.{REGION}.amazonaws.com/{users_pool.user_pool_id}",
            jwt_audience=[dashboard_client.user_pool_client_id],
        )
        http_api = apigwv2.HttpApi(
            scope,
            "HttpApi",
            api_name=http_api_name,
            # SECURITY EXCEPTION (accepted_with_reason): public endpoint by
            # design -- the SPA (a browser) has to reach it directly, with
            # no VPC or network-level gate available to it. What restricts
            # it: default_authorizer below applies the Cognito JWT
            # authorizer to every route added by this stack (see
            # test_every_api_route_has_the_jwt_authorizer); there is no
            # route anywhere in this stack that opts out of it, and CORS
            # is limited to this distribution's own domain.
            description=(
                "outbound-ivr dashboard API: public endpoint, every route requires "
                "a Cognito JWT (default_authorizer, no per-route opt-out)"
            ),
            default_authorizer=jwt_authorizer,
            # No default stage here: created explicitly below (as
            # `DefaultStage`) so it can carry throttling -- `HttpApi` itself
            # has no throttle prop, only `HttpStage` does.
            create_default_stage=False,
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=[cloudfront_domain],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PATCH,
                ],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.minutes(5),
            ),
        )
        NagSuppressions.add_resource_suppressions(
            http_api,
            [
                {
                    "id": "AwsSolutions-APIG1",
                    "reason": "No access logging: demo API, no log consumer.",
                }
            ],
            apply_to_children=True,
        )

        integration = apigwv2_integrations.HttpLambdaIntegration("ApiIntegration", api_lambda)
        # POST /api/calls is the one route that triggers billable work (an
        # AgentCore Runtime invocation that places a real outbound phone call),
        # so it gets a lower per-route throttle below, on top of every route's
        # default-stage throttle and Cognito JWT auth.
        start_call_route_key = "POST /api/calls"
        for path, methods in (
            ("/api/cases", [apigwv2.HttpMethod.GET]),
            ("/api/cases/{id}", [apigwv2.HttpMethod.GET]),
            ("/api/cases/{id}", [apigwv2.HttpMethod.PATCH]),
            ("/api/calls/{call}/events", [apigwv2.HttpMethod.GET]),
            ("/api/calls/{call}/monitor", [apigwv2.HttpMethod.GET]),
            ("/api/recordings/{call}", [apigwv2.HttpMethod.GET]),
            ("/api/calls", [apigwv2.HttpMethod.POST]),
        ):
            http_api.add_routes(path=path, methods=methods, integration=integration)

        # ------------------------------------------------------------------
        # Throttling: every route is both authenticated (default_authorizer,
        # above) and throttled -- an authenticated caller still can't flood
        # this API or the billable work behind POST /api/calls. 10 rps/20
        # burst stage-wide default, tightened to 2 rps/4 burst for the
        # start-call route specifically (`route_settings`, an L1 property --
        # `HttpStage` itself has no per-route throttle prop, only the
        # stage-wide `throttle` used for `default_route_settings` below).
        # ------------------------------------------------------------------
        default_stage = apigwv2.HttpStage(
            scope,
            "DefaultStage",
            http_api=http_api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigwv2.ThrottleSettings(rate_limit=10, burst_limit=20),
        )
        cfn_stage = default_stage.node.default_child
        cfn_stage.add_property_override(
            "RouteSettings",
            {
                start_call_route_key: {
                    "ThrottlingRateLimit": 2,
                    "ThrottlingBurstLimit": 4,
                }
            },
        )
        # `DefaultStage` is a sibling of `http_api`, not one of its children (it has
        # to be, to carry a throttle prop `HttpApi` itself doesn't expose), so the
        # http_api suppression above (apply_to_children=True) never reaches it --
        # same AwsSolutions-APIG1 reason, applied here directly.
        NagSuppressions.add_resource_suppressions(
            default_stage,
            [
                {
                    "id": "AwsSolutions-APIG1",
                    "reason": "No access logging: demo API, no log consumer.",
                }
            ],
        )

        api_url = http_api.api_endpoint

        # ------------------------------------------------------------------
        # Deploy the built SPA plus config.json alongside it, in one
        # BucketDeployment so both land atomically and the CloudFront
        # cache gets invalidated once.
        # ------------------------------------------------------------------
        runtime_config = build_runtime_config(
            api_url=api_url,
            # dashboard/src/lib/auth.ts builds `https://${cognitoDomain}` itself,
            # so this needs to be the bare host, not domain_name's bare prefix
            # (UserPoolDomain.domain_name is just the Cognito-managed-domain
            # prefix, not the full *.auth.<region>.amazoncognito.com host).
            cognito_domain=f"{users_domain.domain_name}.auth.{REGION}.amazoncognito.com",
            cognito_client_id=dashboard_client.user_pool_client_id,
            cognito_region=REGION,
            region=REGION,
            ccp_url=ccp_url,
            connect_region=connect_region,
        )
        s3deploy.BucketDeployment(
            scope,
            "DeploySite",
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            sources=[
                s3deploy.Source.asset(dashboard_dist_dir),
                s3deploy.Source.json_data("config.json", runtime_config),
            ],
        )
        # BucketDeployment's own staging Lambda is CDK's shared, stack-level
        # singleton custom-resource provider (same "Custom::..." pattern as
        # AwsCustomResource elsewhere in this app) -- it's parented directly
        # under the stack, not under "DeploySite", so a suppression on
        # deploy_site itself can't reach it; this needs a stack-wide
        # suppression instead, same as the Chime/Connect custom resources
        # in the other stacks.
        NagSuppressions.add_stack_suppressions(
            Stack.of(scope),
            [
                {
                    "id": "AwsSolutions-L1",
                    "reason": (
                        "CDK-internal BucketDeployment staging Lambda; "
                        "its runtime version is CDK's own choice, not "
                        "configurable from this stack."
                    ),
                },
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWSLambdaBasicExecutionRole, auto-attached by "
                        "the same CDK-internal construct; no "
                        "customer-authored equivalent."
                    ),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "CDK-internal BucketDeployment grants its own "
                        "staging Lambda broad S3 read/write on the "
                        "destination bucket (needed to sync/clear "
                        "arbitrary deployed object keys) and the CDK "
                        "asset bucket (to download the uploaded site "
                        "bundle); this is the standard, unmodified shape "
                        "of that construct, not something this stack "
                        "chose to widen."
                    ),
                    "appliesTo": [
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                        "Action::s3:Abort*",
                        "Action::s3:DeleteObject*",
                        "Resource::*",
                        {"regex": "/^Resource::arn:.*:s3:::cdk-hnb659fds-assets.*\\*$/"},
                        {"regex": "/^Resource::<SiteBucket.*\\.Arn>\\/\\*$/"},
                    ],
                },
            ],
        )

        self.distribution = distribution
        self.http_api = http_api
        self.users_pool = users_pool
