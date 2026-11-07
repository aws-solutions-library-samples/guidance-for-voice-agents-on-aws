"""Assertions for outbound-ivr-dashboard: every route still carries the Cognito
JWT authorizer, the default stage and the billable start-call route both carry
throttling, no calls queue IAM/env (this app has none), and the API Lambda
can call bedrock-agentcore:InvokeAgentRuntime on exactly the Outbound IVR Runtime (plus
its runtime-endpoint children) -- never a wildcard.
"""

from __future__ import annotations

import json


def _iter_resource_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_resource_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_resource_strings(item)


def test_every_api_route_has_the_jwt_authorizer(dashboard_template) -> None:
    authorizers = dashboard_template.find_resources("AWS::ApiGatewayV2::Authorizer")
    assert len(authorizers) == 1
    (authorizer_id, authorizer) = next(iter(authorizers.items()))
    assert authorizer["Properties"]["AuthorizerType"] == "JWT"

    routes = dashboard_template.find_resources("AWS::ApiGatewayV2::Route")
    assert len(routes) == 7
    for logical_id, route in routes.items():
        assert route["Properties"].get("AuthorizerId") == {"Ref": authorizer_id}, (
            f"{logical_id} ({route['Properties'].get('RouteKey')}) is missing the JWT authorizer"
        )


def test_default_stage_has_rate_and_burst_throttling(dashboard_template) -> None:
    (stage,) = dashboard_template.find_resources("AWS::ApiGatewayV2::Stage").values()
    assert stage["Properties"]["StageName"] == "$default"
    assert stage["Properties"]["AutoDeploy"] is True
    default_route_settings = stage["Properties"]["DefaultRouteSettings"]
    assert default_route_settings["ThrottlingRateLimit"] == 10
    assert default_route_settings["ThrottlingBurstLimit"] == 20


def test_start_call_route_has_a_lower_throttle_than_the_stage_default(
    dashboard_template,
) -> None:
    # POST /api/calls is the one route that triggers billable work (an AgentCore
    # Runtime invocation placing a real outbound phone call), so it gets a lower
    # limit than every other route's stage-wide default.
    (stage,) = dashboard_template.find_resources("AWS::ApiGatewayV2::Stage").values()
    route_settings = stage["Properties"]["RouteSettings"]["POST /api/calls"]
    default_route_settings = stage["Properties"]["DefaultRouteSettings"]
    assert route_settings["ThrottlingRateLimit"] < default_route_settings["ThrottlingRateLimit"]
    assert route_settings["ThrottlingBurstLimit"] < default_route_settings["ThrottlingBurstLimit"]


def test_api_lambda_role_has_no_wildcard_resources(dashboard_template) -> None:
    policies = dashboard_template.find_resources("AWS::IAM::Policy")
    api_role_policies = {
        logical_id: res
        for logical_id, res in policies.items()
        if logical_id.startswith("ApiLambdaRoleDefaultPolicy")
    }
    assert api_role_policies, "expected to find the API Lambda role's default policy"
    for logical_id, policy in api_role_policies.items():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            for candidate in _iter_resource_strings(statement["Resource"]):
                assert candidate != "*", f"{logical_id} grants a bare '*' resource"


def test_api_lambda_can_invoke_the_outbound_ivr_runtime_scoped_not_wildcarded(
    dashboard_template,
) -> None:
    policies = dashboard_template.find_resources("AWS::IAM::Policy")
    found = False
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Sid") != "InvokeOutboundIvrRuntime":
                continue
            found = True
            assert statement["Action"] == "bedrock-agentcore:InvokeAgentRuntime"
            resources = (
                statement["Resource"]
                if isinstance(statement["Resource"], list)
                else [statement["Resource"]]
            )
            # Exactly two resources: the Runtime's own ARN, and its
            # runtime-endpoint children -- never a bare "*".
            assert len(resources) == 2
            for resource in resources:
                assert resource != "*"
    assert found, "expected an InvokeOutboundIvrRuntime IAM statement"


def test_api_lambda_env_has_runtime_arn_and_no_calls_queue_url(dashboard_template) -> None:
    functions = dashboard_template.find_resources("AWS::Lambda::Function")
    (api_lambda,) = [
        f
        for f in functions.values()
        if f["Properties"].get("FunctionName") == "outbound-ivr-dashboard-api"
    ]
    env = api_lambda["Properties"]["Environment"]["Variables"]
    assert "OUTBOUND_IVR_RUNTIME_ARN" in env
    assert "OUTBOUND_IVR_CALLS_QUEUE_URL" not in env


def test_no_calls_queue_iam_statement(dashboard_template) -> None:
    policies = dashboard_template.find_resources("AWS::IAM::Policy")
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            assert statement.get("Sid") != "QueueCalls"


def test_site_bucket_blocks_public_access(dashboard_template) -> None:
    buckets = dashboard_template.find_resources("AWS::S3::Bucket")
    (bucket,) = [
        b
        for b in buckets.values()
        if b["Properties"].get("BucketName", "").startswith("outbound-ivr-dashboard-")
    ]
    assert bucket["Properties"]["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }


def test_monitor_route_exists_and_is_always_on(dashboard_template) -> None:
    """Monitor has no .env/context gate any more (private-monitor-s3-spec.md):
    the route is added unconditionally, like every other route, and
    OUTBOUND_IVR_MONITOR_BUCKET is always present in the API Lambda's environment
    because outbound_ivr_stack.py always builds Monitor and always passes its
    bucket into DashboardNestedStack. The old ticket route/env is gone."""
    routes = dashboard_template.find_resources("AWS::ApiGatewayV2::Route")
    monitor_routes = [
        r for r in routes.values() if r["Properties"]["RouteKey"] == "GET /api/calls/{call}/monitor"
    ]
    assert len(monitor_routes) == 1
    ticket_routes = [
        r
        for r in routes.values()
        if r["Properties"]["RouteKey"] == "GET /api/calls/{call}/monitor-ticket"
    ]
    assert ticket_routes == []

    functions = dashboard_template.find_resources("AWS::Lambda::Function")
    (api_lambda,) = [
        f
        for f in functions.values()
        if f["Properties"].get("FunctionName") == "outbound-ivr-dashboard-api"
    ]
    env = api_lambda["Properties"]["Environment"]["Variables"]
    assert "OUTBOUND_IVR_MONITOR_BUCKET" in env
    assert "OUTBOUND_IVR_MONITOR_TABLE" not in env
    assert "OUTBOUND_IVR_MONITOR_WS_URL" not in env
    assert "LIVEKIT_API_KEY" not in env
    assert "LIVEKIT_API_SECRET_ARN" not in env
    assert "LIVEKIT_MONITOR_URL" not in env


def test_dashboard_lambda_can_poll_the_monitor_bucket_scoped_to_its_prefixes(
    dashboard_template,
) -> None:
    """The monitor-bucket IAM is granted directly inside
    dashboard_construct.py's `DashboardConstruct` (same nested stack as the
    role), not a cross-nested-stack grant at outbound_ivr_stack.py's
    composition root. See that file's
    ListMonitorSegments/ReadMonitorObjects/WriteMonitorWatchMarkers
    statements."""
    policies = dashboard_template.find_resources("AWS::IAM::Policy")
    sids_to_statement = {}
    for lid, policy in policies.items():
        if "ApiLambdaRole" not in lid:
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Sid"):
                sids_to_statement[statement["Sid"]] = statement

    listing = sids_to_statement["ListMonitorSegments"]
    assert listing["Action"] == "s3:ListBucket"
    assert "/seg/*" in json.dumps(listing["Condition"])

    read = sids_to_statement["ReadMonitorObjects"]
    assert read["Action"] == "s3:GetObject"
    rendered = json.dumps(read["Resource"])
    assert "/seg/*" in rendered
    assert "/watch/*" in rendered

    write = sids_to_statement["WriteMonitorWatchMarkers"]
    assert write["Action"] == "s3:PutObject"
    assert "/watch/*" in json.dumps(write["Resource"])


def test_users_pool_has_no_self_signup_and_requires_totp_mfa(dashboard_template) -> None:
    from aws_cdk.assertions import Match

    dashboard_template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "AdminCreateUserConfig": Match.object_like({"AllowAdminCreateUserOnly": True}),
            "MfaConfiguration": "ON",
            "EnabledMfas": ["SOFTWARE_TOKEN_MFA"],
        },
    )
