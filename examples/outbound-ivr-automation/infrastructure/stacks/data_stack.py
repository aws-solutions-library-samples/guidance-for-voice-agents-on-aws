"""outbound-ivr-data: cases/offices/events tables and the artifacts bucket, for an
agent that reads/writes AWS directly instead of through local files -- see
docs/design.md.

Reuses this app's own `data_construct.py`'s `DataConstruct` with
`name_prefix="outbound-ivr"` and no calls queue: this app's dashboard Lambda
calls `bedrock-agentcore:InvokeAgentRuntime` directly (see dashboard_stack.py
and agent_runtime_stack.py), so there is no dispatcher to drain a queue.
"""

from __future__ import annotations

from aws_cdk import NestedStack, Tags
from constructs import Construct

from .constants import PROJECT_TAG, RESOURCE_PREFIX
from .data_construct import DataConstruct


class DataNestedStack(NestedStack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        data = DataConstruct(self, name_prefix=RESOURCE_PREFIX, include_calls_queue=False)
        self.kms_key = data.kms_key
        self.cases_table = data.cases_table
        self.offices_table = data.offices_table
        self.events_table = data.events_table
        self.artifacts_bucket = data.artifacts_bucket

        Tags.of(self).add("project", PROJECT_TAG)
