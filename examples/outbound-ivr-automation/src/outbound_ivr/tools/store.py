"""Storage interface for cases and offices, plus artifacts.

One interface, two implementations: an in-memory store for tests, and a DynamoDB-backed
store for AWS. See docs/design.md, "Data".

Synthetic data only.
"""

from __future__ import annotations

import copy
import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

FINAL_STATUSES = {"complete", "escalated", "handed off"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decimals_to_native(obj: Any) -> Any:
    """boto3's DynamoDB resource returns numbers as Decimal; make them plain int/float."""
    if isinstance(obj, list):
        return [_decimals_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _decimals_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def default_case(case_id: str, **fields: Any) -> dict:
    """A case item with every field the store expects, defaults filled in.

    Shape: case_id (key), payer, patient fields, member_id, auth_ref, procedure,
    ordering_provider, office_id, missing_items (key, label, question), status,
    attempts, results (per key: value, confirmed, at, call), outcome, updated_at.
    """
    case = {
        "case_id": case_id,
        "payer": "",
        "patient_name": "",
        "patient_dob": "",
        "member_id": "",
        "auth_ref": "",
        "procedure": "",
        "ordering_provider": "",
        "office_id": "",
        "missing_items": [],
        "status": "pending",
        "attempts": 0,
        "results": {},
        "outcome": None,
        "updated_at": _now(),
    }
    case.update(fields)
    case.setdefault("results", {})
    case.setdefault("missing_items", [])
    return case


def default_office(office_id: str, **fields: Any) -> dict:
    office = {
        "office_id": office_id,
        "name": "",
        "number": "",
        "hours": "",
    }
    office.update(fields)
    return office


class CaseStore(ABC):
    """Cases and offices, keyed by case_id / office_id."""

    @abstractmethod
    def get_case(self, case_id: str) -> dict | None: ...

    @abstractmethod
    def put_case(self, case: dict) -> None:
        """Create or overwrite a case item. Used for seeding, not by the tools."""

    @abstractmethod
    def record_result(
        self, case_id: str, call: str, key: str, value: str, confirmed: bool
    ) -> dict | None:
        """Store one answer under results[key]. Returns the updated case, or None if unknown."""

    @abstractmethod
    def set_status(self, case_id: str, status: str) -> None: ...

    @abstractmethod
    def increment_attempts(self, case_id: str) -> int | None:
        """Add one to attempts. Returns the new count, or None if unknown."""

    @abstractmethod
    def set_outcome(self, case_id: str, outcome: dict, status: str) -> bool:
        """Set the outcome and status, unless the case already has a final outcome.

        Returns True if applied, False if the case already has a final (complete or
        escalated) outcome -- callers must not overwrite that.
        """

    @abstractmethod
    def get_office(self, office_id: str) -> dict | None: ...

    @abstractmethod
    def put_office(self, office: dict) -> None: ...


class InMemoryCaseStore(CaseStore):
    """For unit tests and for running the demo without DynamoDB Local."""

    def __init__(self) -> None:
        self._cases: dict[str, dict] = {}
        self._offices: dict[str, dict] = {}

    def get_case(self, case_id: str) -> dict | None:
        case = self._cases.get(case_id)
        return copy.deepcopy(case) if case is not None else None

    def put_case(self, case: dict) -> None:
        case = default_case(case["case_id"], **{k: v for k, v in case.items() if k != "case_id"})
        self._cases[case["case_id"]] = copy.deepcopy(case)

    def record_result(
        self, case_id: str, call: str, key: str, value: str, confirmed: bool
    ) -> dict | None:
        case = self._cases.get(case_id)
        if case is None:
            return None
        case["results"][key] = {
            "value": value,
            "confirmed": bool(confirmed),
            "at": _now(),
            "call": call,
        }
        case["updated_at"] = _now()
        return copy.deepcopy(case)

    def set_status(self, case_id: str, status: str) -> None:
        case = self._cases.get(case_id)
        if case is not None:
            case["status"] = status
            case["updated_at"] = _now()

    def increment_attempts(self, case_id: str) -> int | None:
        case = self._cases.get(case_id)
        if case is None:
            return None
        case["attempts"] = int(case.get("attempts", 0)) + 1
        case["updated_at"] = _now()
        return case["attempts"]

    def set_outcome(self, case_id: str, outcome: dict, status: str) -> bool:
        case = self._cases.get(case_id)
        if case is None:
            return False
        if case.get("status") in FINAL_STATUSES:
            return False
        case["outcome"] = copy.deepcopy(outcome)
        case["status"] = status
        case["updated_at"] = _now()
        return True

    def get_office(self, office_id: str) -> dict | None:
        office = self._offices.get(office_id)
        return copy.deepcopy(office) if office is not None else None

    def put_office(self, office: dict) -> None:
        office = default_office(
            office["office_id"], **{k: v for k, v in office.items() if k != "office_id"}
        )
        self._offices[office["office_id"]] = copy.deepcopy(office)


class DynamoDBCaseStore(CaseStore):
    """Backed by DynamoDB (or DynamoDB Local, via endpoint_url).

    Table names default to the AWS names from the design doc; both are overridable so
    the same code points at DynamoDB Local in the demo and DynamoDB in AWS.
    """

    def __init__(
        self,
        cases_table: str | None = None,
        offices_table: str | None = None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ) -> None:
        import boto3

        self.cases_table_name = cases_table or os.environ.get(
            "OUTBOUND_IVR_CASES_TABLE", "outbound-ivr-cases"
        )
        self.offices_table_name = offices_table or os.environ.get(
            "OUTBOUND_IVR_OFFICES_TABLE", "outbound-ivr-offices"
        )
        endpoint_url = endpoint_url or os.environ.get("OUTBOUND_IVR_DYNAMODB_ENDPOINT")
        # Lambda and AgentCore Runtime both set AWS_REGION to their own deployment
        # region, which is always where these tables live.
        region_name = region_name or os.environ["AWS_REGION"]
        self._resource = boto3.resource(
            "dynamodb", endpoint_url=endpoint_url, region_name=region_name
        )
        self._cases = self._resource.Table(self.cases_table_name)
        self._offices = self._resource.Table(self.offices_table_name)

    def get_case(self, case_id: str) -> dict | None:
        resp = self._cases.get_item(Key={"case_id": case_id})
        item = resp.get("Item")
        return _decimals_to_native(item) if item is not None else None

    def put_case(self, case: dict) -> None:
        case = default_case(case["case_id"], **{k: v for k, v in case.items() if k != "case_id"})
        self._cases.put_item(Item=case)

    def record_result(
        self, case_id: str, call: str, key: str, value: str, confirmed: bool
    ) -> dict | None:
        from botocore.exceptions import ClientError

        now = _now()
        try:
            resp = self._cases.update_item(
                Key={"case_id": case_id},
                UpdateExpression=(
                    "SET results = if_not_exists(results, :empty_map), updated_at = :now"
                ),
                ConditionExpression="attribute_exists(case_id)",
                ExpressionAttributeValues={":empty_map": {}, ":now": now},
            )
            resp = self._cases.update_item(
                Key={"case_id": case_id},
                UpdateExpression="SET results.#key = :item, updated_at = :now",
                ConditionExpression="attribute_exists(case_id)",
                ExpressionAttributeNames={"#key": key},
                ExpressionAttributeValues={
                    ":item": {
                        "value": value,
                        "confirmed": bool(confirmed),
                        "at": now,
                        "call": call,
                    },
                    ":now": now,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
        return _decimals_to_native(resp.get("Attributes"))

    def set_status(self, case_id: str, status: str) -> None:
        self._cases.update_item(
            Key={"case_id": case_id},
            UpdateExpression="SET #status = :status, updated_at = :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": status, ":now": _now()},
        )

    def increment_attempts(self, case_id: str) -> int | None:
        from botocore.exceptions import ClientError

        try:
            resp = self._cases.update_item(
                Key={"case_id": case_id},
                UpdateExpression="SET attempts = if_not_exists(attempts, :zero) + :one, updated_at = :now",
                ConditionExpression="attribute_exists(case_id)",
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": _now()},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
        return int(resp["Attributes"]["attempts"])

    def set_outcome(self, case_id: str, outcome: dict, status: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._cases.update_item(
                Key={"case_id": case_id},
                UpdateExpression="SET outcome = :outcome, #status = :status, updated_at = :now",
                ConditionExpression=(
                    "attribute_exists(case_id) AND "
                    "(attribute_not_exists(#status) OR "
                    "(#status <> :complete AND #status <> :escalated AND #status <> :handed_off))"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":outcome": outcome,
                    ":status": status,
                    ":now": _now(),
                    ":complete": "complete",
                    ":escalated": "escalated",
                    ":handed_off": "handed off",
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def get_office(self, office_id: str) -> dict | None:
        resp = self._offices.get_item(Key={"office_id": office_id})
        item = resp.get("Item")
        return _decimals_to_native(item) if item is not None else None

    def put_office(self, office: dict) -> None:
        office = default_office(
            office["office_id"], **{k: v for k, v in office.items() if k != "office_id"}
        )
        self._offices.put_item(Item=office)

    @staticmethod
    def ensure_tables(
        cases_table: str,
        offices_table: str,
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ) -> dict[str, bool]:
        """Create the cases and offices tables if they don't exist. Returns what was created."""
        import boto3
        from botocore.exceptions import ClientError

        # Lambda and AgentCore Runtime both set AWS_REGION to their own deployment
        # region; callers running outside those (a deploy-time script) pass it explicitly.
        region_name = region_name or os.environ["AWS_REGION"]
        client = boto3.client("dynamodb", endpoint_url=endpoint_url, region_name=region_name)
        created = {}
        for name, key_name, gsi in (
            (cases_table, "case_id", True),
            (offices_table, "office_id", False),
        ):
            try:
                client.describe_table(TableName=name)
                created[name] = False
                continue
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
            kwargs: dict[str, Any] = {
                "TableName": name,
                "AttributeDefinitions": [{"AttributeName": key_name, "AttributeType": "S"}],
                "KeySchema": [{"AttributeName": key_name, "KeyType": "HASH"}],
                "BillingMode": "PAY_PER_REQUEST",
            }
            if gsi:
                kwargs["AttributeDefinitions"].append(
                    {"AttributeName": "status", "AttributeType": "S"}
                )
                kwargs["GlobalSecondaryIndexes"] = [
                    {
                        "IndexName": "status-index",
                        "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                ]
            client.create_table(**kwargs)
            client.get_waiter("table_exists").wait(TableName=name)
            created[name] = True
        return created


class ArtifactStore(ABC):
    """Transcripts, case records and recordings: a local folder locally, S3 in AWS."""

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        """Store an artifact. Returns a locator (a file path or an s3:// URI)."""

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...


class LocalArtifactStore(ArtifactStore):
    """Artifacts under a local folder, `runs/` by default."""

    def __init__(self, root: str | os.PathLike | None = None) -> None:
        self.root = Path(root or os.environ.get("OUTBOUND_IVR_ARTIFACTS_DIR", "runs/artifacts"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise ValueError(f"artifact key escapes the artifacts root: {key!r}")
        return path

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3ArtifactStore(ArtifactStore):
    """Artifacts in S3, `OUTBOUND_IVR_ARTIFACTS_BUCKET`."""

    def __init__(
        self, bucket: str | None = None, prefix: str = "", region_name: str | None = None
    ) -> None:
        import boto3

        self.bucket = bucket or os.environ["OUTBOUND_IVR_ARTIFACTS_BUCKET"]
        self.prefix = prefix
        self._client = boto3.client("s3", region_name=region_name or os.environ.get("AWS_REGION"))

    def _full_key(self, key: str) -> str:
        return f"{self.prefix}{key}" if self.prefix else key

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": self._full_key(key), "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        self._client.put_object(**kwargs)
        return f"s3://{self.bucket}/{self._full_key(key)}"

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self.bucket, Key=self._full_key(key))
        return resp["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=self._full_key(key))
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._full_key(key)},
            ExpiresIn=expires_in,
        )


def case_store_from_env() -> CaseStore:
    """DynamoDB store configured from the environment -- the production default."""
    return DynamoDBCaseStore()


def artifact_store_from_env() -> ArtifactStore:
    """S3 if `OUTBOUND_IVR_ARTIFACTS_BUCKET` is set, else a local folder under `runs/`."""
    if os.environ.get("OUTBOUND_IVR_ARTIFACTS_BUCKET"):
        return S3ArtifactStore()
    return LocalArtifactStore()
