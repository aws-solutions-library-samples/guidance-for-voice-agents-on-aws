"""outbound-ivr-monitor: the private Monitor transport. This module builds exactly
one piece of the design: one S3 bucket, exposed as `self.bucket`.

**S3 segments, not a WebSocket.** The browser never talks to this bucket
directly -- no presigned URLs, no bucket CORS, no public endpoint. Every byte
it sees comes through the existing Cognito-protected dashboard HTTP API
(`dashboard_stack.py`'s `GET /api/calls/{call}/monitor`), which polls this
bucket on the API Lambda's own role. The Runtime
(`src/outbound_ivr/voice/monitor.py`'s `MonitorPublisher`) writes segments here only
while a watch marker under the same call's `watch/` prefix is fresh -- see
that module's own docstring for the key shapes both roles read and write.

**One bucket, the data stack's own CMK, no DynamoDB at all.** A single bucket
whose objects mostly expire within a day -- fewer moving parts and a smaller
attack surface than a WebSocket fanout design would need, in exchange for a
3-6 second listen-in delay. Same "one CMK per data layer" convention
`data_construct.py`'s `DataConstruct` already uses for the artifacts bucket --
no second, dedicated key.

**Not versioned, DESTROY.** Every object here is either a zero-byte watch
marker or a segment with a 1-day expiration; there is nothing worth
versioning, and a teardown of a non-empty bucket simply fails (the same
"DESTROY with no auto-delete" choice `data_construct.py`'s own artifacts
bucket makes, for the same reason: no second look before deleting unexpired
segments). `infrastructure/scripts/cleanup.sh` empties this bucket before
destroying the stacks.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    NestedStack,
    RemovalPolicy,
    Tags,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_s3 as s3,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from .constants import ACCOUNT_ID, PROJECT_TAG, RESOURCE_PREFIX


class MonitorNestedStack(NestedStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data_kms_key: kms.Key,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # Bucket: watch markers (`<call_id>/watch/listen`,
        # `<call_id>/watch/levels`) written by the dashboard API Lambda, and
        # segments (`<call_id>/seg/<seq>.json`) written by the Runtime. Both
        # roles' IAM lives where they live -- agent_runtime_stack.py and
        # dashboard_construct.py -- scoped to this bucket and these two key
        # prefixes only, never a wildcard across buckets.
        # ------------------------------------------------------------------
        self.bucket = s3.Bucket(
            self,
            "MonitorBucket",
            bucket_name=f"{RESOURCE_PREFIX}-monitor-{ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=data_kms_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            # Not versioned: every object here is either a zero-byte marker
            # or a short-lived segment, nothing worth keeping old versions
            # of, and a delete (MonitorPublisher.close()'s own best-effort
            # cleanup) must be a real delete, not a new version on top of
            # the old one.
            versioned=False,
            # DESTROY with no auto-delete, same choice data_stack.py's own
            # artifacts bucket makes: objects here expire within a day on
            # their own, so a teardown of a non-empty bucket simply fails
            # rather than silently erasing anything still live.
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="monitor-expiry",
                    enabled=True,
                    expiration=Duration.days(1),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            self.bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": (
                        "Demo bucket, synthetic call audio only, objects "
                        "expire within a day; access logging adds cost and "
                        "a second bucket with no consumer here -- same "
                        "reasoning as data_stack.py's own artifacts bucket."
                    ),
                }
            ],
        )

        Tags.of(self).add("project", PROJECT_TAG)
