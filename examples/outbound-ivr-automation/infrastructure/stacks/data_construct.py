"""outbound-ivr-data: cases/offices/events tables, the artifacts bucket, the calls queue.

Every access pattern here is by key (get a case, update it, list pending
cases through a GSI on status, look up an office) -- see docs/design.md for
why this is DynamoDB and not a relational store.

The resource-building logic lives in `DataConstruct`, a plain holder (not a
`Construct` subclass -- same "build directly on the given scope" convention
as `network_stack.py`'s `NetworkStack`) so `data_stack.py`'s `DataNestedStack`
can reuse the exact same table/bucket definitions (outbound-ivr-prefixed, no
calls queue) without duplicating this file.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from cdk_nag import NagSuppressions
from constructs import Construct


class DataConstruct:
    """Builds the cases/offices/events tables, the artifacts bucket, and
    (optionally) the calls queue directly on `scope` -- not nested one level
    deeper, so logical IDs are identical to building them straight in a Stack.

    `name_prefix` becomes `<name_prefix>-cases`/`-offices`/`-events`/
    `-artifacts-<account>` -- `data_stack.py`'s `DataNestedStack` passes
    `RESOURCE_PREFIX` ("outbound-ivr"). `include_calls_queue` is False there:
    the LiveKit/AgentCore path calls `InvokeAgentRuntime` directly, with no
    dispatcher to drain a queue.

    `retain_data` (context `retainData`) switches the three tables to
    `RemovalPolicy.RETAIN`. It defaults to False: `cdk destroy` takes the
    synthetic cases with it, and the next deploy does not collide with an
    orphaned table of the same name. Pass it when the case data in an
    environment is worth more than a clean teardown.
    """

    def __init__(
        self,
        scope: Construct,
        *,
        name_prefix: str,
        include_calls_queue: bool = True,
        retain_data: bool | None = None,
    ) -> None:
        # None means "ask the app's context", so neither wrapper has to thread
        # the flag through explicitly.
        if retain_data is None:
            retain_data = bool(scope.node.try_get_context("retainData"))
        table_removal_policy = RemovalPolicy.RETAIN if retain_data else RemovalPolicy.DESTROY
        artifact_retention_days = int(scope.node.try_get_context("artifactRetentionDays") or 180)
        # One CMK for both tables and the bucket: demo scale, one blast
        # radius to reason about, one key policy to review.
        self.kms_key = kms.Key(
            scope,
            "DataKey",
            alias=f"{name_prefix}-data",
            description=f"{name_prefix} cases/offices tables and the artifacts bucket",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.cases_table = dynamodb.Table(
            scope,
            "CasesTable",
            table_name=f"{name_prefix}-cases",
            partition_key=dynamodb.Attribute(name="case_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            # DESTROY by default: synthetic demo data, cheap to regenerate, and
            # an orphaned table would block the next deploy on a name collision.
            # A production case-record table should RETAIN: pass
            # `-c retainData=true`.
            removal_policy=table_removal_policy,
        )
        self.cases_table.add_global_secondary_index(
            index_name="status",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
        )

        self.offices_table = dynamodb.Table(
            scope,
            "OfficesTable",
            table_name=f"{name_prefix}-offices",
            partition_key=dynamodb.Attribute(name="office_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=table_removal_policy,
        )

        # <prefix>-events: the per-call event log (dtmf, heard/said,
        # status...), written by whatever runs the call (the telephony
        # dispatcher in infra/'s design, the AgentCore Runtime in the
        # CDK app's) and read later by the dashboard API. Keyed for one query
        # pattern: all events for a call, in order. No PITR -- short-lived,
        # TTL'd event rows, not evidence (that's the case record in
        # S3/DynamoDB).
        self.events_table = dynamodb.Table(
            scope,
            "EventsTable",
            table_name=f"{name_prefix}-events",
            partition_key=dynamodb.Attribute(name="call", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="seq", type=dynamodb.AttributeType.NUMBER),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
            time_to_live_attribute="expires_at",
            # Always DESTROY, deliberately not following `retainData`: these rows
            # expire on their own within days, so retaining the table past a
            # teardown preserves nothing worth having and leaves a name collision
            # behind. The same reasoning as the DDB3 suppression below.
            removal_policy=RemovalPolicy.DESTROY,
        )
        NagSuppressions.add_resource_suppressions(
            self.events_table,
            [
                {
                    "id": "AwsSolutions-DDB3",
                    "reason": (
                        "Short-lived, TTL'd event rows (dtmf, heard/said, "
                        "status...), not evidence -- the case record in "
                        "S3/DynamoDB is the thing worth restoring to a "
                        "point in time. Point-in-time recovery for a "
                        "table whose rows expire on their own within days "
                        "adds cost with nothing meaningful to recover."
                    ),
                }
            ],
        )

        self.artifacts_bucket = s3.Bucket(
            scope,
            "ArtifactsBucket",
            bucket_name=f"{name_prefix}-artifacts-{Stack.of(scope).account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            versioned=True,
            # DESTROY with no `auto_delete_objects`, which in practice means a
            # `cdk destroy` of a non-empty versioned bucket FAILS rather than
            # taking the recordings with it. That is the safer failure for a
            # bucket holding real call audio, but it is a failure: empty the
            # bucket (objects and versions) by hand first. Deliberately not
            # switched to auto-delete, which would hand a teardown the power to
            # erase recordings with no second look.
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="artifact-retention",
                    enabled=True,
                    # How long to keep transcripts, recordings and evidence is a
                    # compliance decision, not a default. 180 days keeps a demo
                    # bucket from growing forever; `-c artifactRetentionDays=<n>`
                    # is the lever for an environment that has actually made the
                    # decision. The rule id used to be "placeholder-retention",
                    # which read as deliberate to anyone who did not open this
                    # file.
                    expiration=Duration.days(artifact_retention_days),
                    noncurrent_version_expiration=Duration.days(min(30, artifact_retention_days)),
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            self.artifacts_bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": (
                        "Demo bucket, synthetic data only, no log consumer "
                        "for a short-lived demo; access logging adds cost "
                        "and a second bucket for no benefit here."
                    ),
                }
            ],
        )

        self.calls_dlq: sqs.Queue | None = None
        self.calls_queue: sqs.Queue | None = None
        if include_calls_queue:
            self.calls_dlq = sqs.Queue(
                scope,
                "CallsDeadLetterQueue",
                queue_name=f"{name_prefix}-calls-dlq",
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=self.kms_key,
                retention_period=Duration.days(14),
                enforce_ssl=True,
                removal_policy=RemovalPolicy.DESTROY,
            )
            self.calls_queue = sqs.Queue(
                scope,
                "CallsQueue",
                queue_name=f"{name_prefix}-calls",
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=self.kms_key,
                visibility_timeout=Duration.minutes(10),
                dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=self.calls_dlq),
                enforce_ssl=True,
                removal_policy=RemovalPolicy.DESTROY,
            )
