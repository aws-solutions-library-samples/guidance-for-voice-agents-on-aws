"""outbound-ivr-dashboard: the hosted dashboard SPA, its API, and its own Cognito
user pool -- reusing this app's own `dashboard_construct.py`'s `DashboardConstruct`,
wired to this app's own tables/bucket (data_stack.py), no calls queue, and the agent
Runtime's ARN so the API Lambda can call `bedrock-agentcore:InvokeAgentRuntime`
directly (see agent_runtime_stack.py and the design plan's shared contract).

**Context-default paths.** `DashboardConstruct`'s own fallbacks for
`dashboardDistDir`/`apiLambdaDir`/`outboundIvrPackageDir` assume the CDK app runs from
`infrastructure/` (one directory below this example's root); this stack sets context
one level up (on itself, via `self.node.set_context`, read before
`DashboardConstruct`'s own `try_get_context` calls) with the right relative paths --
but only when the caller (CLI `-c ...` or `outbound_ivr_stack.py`) hasn't already
supplied an explicit value, so an override still wins.

Also forwards the Monitor nested stack's bucket, and the front desk's CCP
URL, into `DashboardConstruct`, which wires the `GET /api/calls/{call}/
monitor` route's env/IAM -- see monitor_stack.py and dashboard_construct.py.
"""

from __future__ import annotations

from aws_cdk import NestedStack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct

from .constants import ACCOUNT_ID, PROJECT_TAG, REGION, RESOURCE_PREFIX
from .dashboard_construct import DashboardConstruct

# Relative to this app's own working directory (infrastructure/), one level
# below this example's root.
_CONTEXT_DEFAULTS = {
    "dashboardDistDir": "../src/dashboard/dist",
    "apiLambdaDir": "../src/lambdas/api",
    "outboundIvrPackageDir": "../src/outbound_ivr",
}


class DashboardNestedStack(NestedStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cases_table: dynamodb.Table,
        offices_table: dynamodb.Table,
        events_table: dynamodb.Table,
        artifacts_bucket: s3.Bucket,
        data_kms_key: kms.Key,
        agent_runtime_arn: str,
        monitor_bucket: s3.Bucket | None = None,
        ccp_url: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        for key, relative_default in _CONTEXT_DEFAULTS.items():
            if self.node.try_get_context(key) is None:
                self.node.set_context(key, relative_default)

        dashboard = DashboardConstruct(
            self,
            cases_table=cases_table,
            offices_table=offices_table,
            events_table=events_table,
            artifacts_bucket=artifacts_bucket,
            calls_queue=None,
            data_kms_key=data_kms_key,
            bucket_name=f"{RESOURCE_PREFIX}-dashboard-{ACCOUNT_ID}",
            http_api_name=f"{RESOURCE_PREFIX}-dashboard-api",
            users_pool_name=f"{RESOURCE_PREFIX}-dashboard-users",
            users_domain_prefix=f"{RESOURCE_PREFIX}-dashboard-{ACCOUNT_ID}",
            spa_client_name=f"{RESOURCE_PREFIX}-dashboard-spa",
            api_function_name=f"{RESOURCE_PREFIX}-dashboard-api",
            agent_runtime_arn=agent_runtime_arn,
            monitor_bucket=monitor_bucket,
            ccp_url=ccp_url,
            connect_region=REGION,
        )
        self.api_lambda = dashboard.api_lambda
        self.distribution = dashboard.distribution
        self.http_api = dashboard.http_api
        self.users_pool = dashboard.users_pool

        Tags.of(self).add("project", PROJECT_TAG)
