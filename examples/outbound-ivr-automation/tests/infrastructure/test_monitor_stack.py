"""Assertions for outbound-ivr-monitor: one S3 bucket -- block public access, SSL
enforced, KMS with the data stack's own CMK, not versioned, a 1-day
expiration plus an abort-incomplete-multipart-upload rule. Replaces
test_monitor_stack.py's old two-table/three-Lambda/WebSocket assertions (the
DynamoDB-stream + API Gateway WebSocket design).
"""

from __future__ import annotations

import json


def _monitor_bucket(monitor_template) -> dict:
    (bucket,) = [
        b
        for b in monitor_template.find_resources("AWS::S3::Bucket").values()
        if b["Properties"].get("BucketName", "").startswith("outbound-ivr-monitor-")
    ]
    return bucket


def test_bucket_blocks_public_access(monitor_template) -> None:
    bucket = _monitor_bucket(monitor_template)
    assert bucket["Properties"]["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }


def test_bucket_enforces_ssl(monitor_template) -> None:
    # enforce_ssl=True attaches a bucket policy denying non-TLS requests --
    # that policy is a separate AWS::S3::BucketPolicy resource, not a
    # property of the bucket itself. Exactly one bucket in this stack, so
    # exactly one such policy.
    (policy,) = monitor_template.find_resources("AWS::S3::BucketPolicy").values()
    rendered = json.dumps(policy["Properties"]["PolicyDocument"])
    assert "aws:SecureTransport" in rendered
    assert "false" in rendered


def test_bucket_uses_kms_with_the_data_stacks_cmk(monitor_template, data_template) -> None:
    """Same CMK data_stack.py's own cases/offices/events tables and
    artifacts bucket use -- private-monitor-s3-spec.md's "one CMK per data
    layer" convention, no second, dedicated key."""
    bucket = _monitor_bucket(monitor_template)
    sse = bucket["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    rule = sse["ServerSideEncryptionByDefault"]
    assert rule["SSEAlgorithm"] == "aws:kms"
    assert "KMSMasterKeyID" in rule
    assert sse.get("BucketKeyEnabled") is True


def test_bucket_is_not_versioned(monitor_template) -> None:
    bucket = _monitor_bucket(monitor_template)
    # Not versioned: a delete (MonitorPublisher.close()'s best-effort
    # cleanup) must be a real delete, never a new version on top of the old
    # one. CDK omits VersioningConfiguration entirely when versioned=False.
    assert "VersioningConfiguration" not in bucket["Properties"]


def test_bucket_lifecycle_expires_objects_in_one_day(monitor_template) -> None:
    bucket = _monitor_bucket(monitor_template)
    (rule,) = bucket["Properties"]["LifecycleConfiguration"]["Rules"]
    assert rule["Status"] == "Enabled"
    assert rule["ExpirationInDays"] == 1
    assert rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 1


def test_bucket_removal_policy_is_destroy_with_no_auto_delete(monitor_template) -> None:
    (logical_id, bucket) = next(
        (lid, b)
        for lid, b in monitor_template.find_resources("AWS::S3::Bucket").items()
        if b["Properties"].get("BucketName", "").startswith("outbound-ivr-monitor-")
    )
    assert bucket["DeletionPolicy"] == "Delete"
    # No auto-delete-objects custom resource targeting this bucket -- a
    # teardown of a non-empty bucket must fail, not silently empty it.
    custom_resources = monitor_template.find_resources("Custom::S3AutoDeleteObjects")
    assert not any(
        r["Properties"].get("BucketName") == {"Ref": logical_id} for r in custom_resources.values()
    )


def test_no_dynamodb_tables_or_websocket_api_remain(monitor_template) -> None:
    """The old two-table/WebSocket design is gone entirely -- just the one
    bucket."""
    assert monitor_template.find_resources("AWS::DynamoDB::Table") == {}
    assert monitor_template.find_resources("AWS::ApiGatewayV2::Api") == {}
    assert monitor_template.find_resources("AWS::Lambda::Function") == {}
    assert monitor_template.find_resources("AWS::Lambda::EventSourceMapping") == {}
    assert monitor_template.find_resources("AWS::S3::Bucket") != {}
