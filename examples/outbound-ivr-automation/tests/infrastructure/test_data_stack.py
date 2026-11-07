"""Assertions for outbound-ivr-data: outbound-ivr-prefixed physical names (distinct from
infra/'s own `outbound-ivr-legacy-*` names), encrypted + PITR tables, no public S3, and
no calls queue -- the LiveKit/AgentCore path calls InvokeAgentRuntime directly, no
dispatcher to drain one.
"""

from __future__ import annotations

from aws_cdk.assertions import Match


def test_tables_use_outbound_ivr_prefixed_names(data_template) -> None:
    for table_name in ("outbound-ivr-cases", "outbound-ivr-offices", "outbound-ivr-events"):
        data_template.has_resource_properties("AWS::DynamoDB::Table", {"TableName": table_name})


def test_cases_table_encrypted_with_pitr_and_status_gsi(data_template) -> None:
    data_template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "outbound-ivr-cases",
            "KeySchema": Match.array_with([{"AttributeName": "case_id", "KeyType": "HASH"}]),
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
            "SSESpecification": Match.object_like({"SSEEnabled": True, "SSEType": "KMS"}),
            "GlobalSecondaryIndexes": Match.array_with(
                [Match.object_like({"IndexName": "status"})]
            ),
        },
    )


def test_events_table_has_the_right_key_schema_and_ttl(data_template) -> None:
    data_template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "outbound-ivr-events",
            "KeySchema": Match.array_with(
                [
                    {"AttributeName": "call", "KeyType": "HASH"},
                    {"AttributeName": "seq", "KeyType": "RANGE"},
                ]
            ),
            "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
        },
    )


def test_artifacts_bucket_is_outbound_ivr_prefixed_and_blocks_public_access(data_template) -> None:
    buckets = data_template.find_resources("AWS::S3::Bucket")
    (bucket,) = [
        b
        for b in buckets.values()
        if "outbound-ivr-artifacts-" in str(b["Properties"].get("BucketName"))
    ]
    assert bucket["Properties"]["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }


def test_no_calls_queue_in_this_stack(data_template) -> None:
    assert data_template.find_resources("AWS::SQS::Queue") == {}
