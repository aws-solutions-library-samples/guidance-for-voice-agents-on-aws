"""ArtifactStore: a local folder locally, S3 (via moto) in the shape AWS will use."""

import boto3
import pytest
from moto import mock_aws

from outbound_ivr.tools.store import LocalArtifactStore, S3ArtifactStore


def test_local_artifact_store_put_get_exists(tmp_path):
    store = LocalArtifactStore(root=tmp_path)
    assert not store.exists("cases/call-1.json")
    locator = store.put("cases/call-1.json", b'{"ok": true}')
    assert locator.endswith("cases/call-1.json")
    assert store.exists("cases/call-1.json")
    assert store.get("cases/call-1.json") == b'{"ok": true}'


def test_local_artifact_store_rejects_escaping_keys(tmp_path):
    store = LocalArtifactStore(root=tmp_path)
    with pytest.raises(ValueError):
        store.put("../escape.json", b"nope")


@pytest.fixture
def s3_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="outbound-ivr-artifacts-test")
        yield "outbound-ivr-artifacts-test"


def test_s3_artifact_store_put_get_exists(s3_bucket):
    store = S3ArtifactStore(bucket=s3_bucket, prefix="cases/", region_name="us-east-1")
    assert not store.exists("call-1.json")
    locator = store.put("call-1.json", b'{"ok": true}', content_type="application/json")
    assert locator == f"s3://{s3_bucket}/cases/call-1.json"
    assert store.exists("call-1.json")
    assert store.get("call-1.json") == b'{"ok": true}'


def test_s3_artifact_store_presigned_url(s3_bucket):
    store = S3ArtifactStore(bucket=s3_bucket, region_name="us-east-1")
    store.put("recordings/call-1.wav", b"audio")
    url = store.presigned_url("recordings/call-1.wav")
    assert s3_bucket in url
    assert "recordings/call-1.wav" in url
