"""
Webhook Authentication

Authenticates POST /start requests before any room, token, or voice session
is created.

Two request sources are supported:
1. PSTN via Daily pinless dial-in: verified with Daily's HMAC signature
   (X-Pinless-Timestamp / X-Pinless-Signature headers).
2. SIP via direct request (source="sip"): these requests come from your own
   SIP infrastructure, not from Daily, so they must present a shared secret
   in the X-Sip-Webhook-Secret header.

Daily signature scheme, per
https://docs.daily.co/guides/products/dial-in-dial-out/dialin-pinless#verifying-the-webhook-signature-hmac

    signed_payload = X-Pinless-Timestamp + "." + raw_json_body
    signature = base64(HMAC-SHA256(base64_decode(hmac_secret), signed_payload))

Daily's docs do not state the timestamp units, so both epoch seconds and epoch
milliseconds are accepted. Requests whose timestamp is more than
TIMESTAMP_TOLERANCE_SECONDS from the current time are rejected, which stops a
captured request from being replayed later.

Both secrets live in a dedicated Secrets Manager secret (WEBHOOK_SECRET_ARN)
as JSON keys DAILY_HMAC_SECRET and SIP_WEBHOOK_SECRET. The secret is cached
per Lambda container for SECRET_CACHE_TTL_SECONDS so rotated values are picked
up without a redeploy.
"""

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

DAILY_TIMESTAMP_HEADER = "x-pinless-timestamp"
DAILY_SIGNATURE_HEADER = "x-pinless-signature"
SIP_SECRET_HEADER = "x-sip-webhook-secret"

DAILY_HMAC_SECRET_KEY = "DAILY_HMAC_SECRET"
SIP_WEBHOOK_SECRET_KEY = "SIP_WEBHOOK_SECRET"

# Maximum allowed difference between the request timestamp and now
TIMESTAMP_TOLERANCE_SECONDS = 300
# How long a fetched secret is reused within one Lambda container
SECRET_CACHE_TTL_SECONDS = 300
# Timestamps above this are treated as epoch milliseconds (year 5138 in seconds)
_EPOCH_MILLIS_THRESHOLD = 100_000_000_000

# Value written by the CDK secrets construct until the operator sets a real one
_PLACEHOLDER_VALUE = "PLACEHOLDER_REPLACE_AFTER_DEPLOY"

_secrets_client: Any = None
_cached_secrets: dict | None = None
_cache_expires_at = 0.0


class WebhookAuthError(Exception):
    """Request was not authenticated. Carries the HTTP status and a log reason."""

    def __init__(self, status_code: int, reason: str, detail: str = ""):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason
        self.detail = detail


def authenticate_request(event: dict, body: dict) -> str:
    """
    Authenticate a /start request.

    Args:
        event: API Gateway proxy event (headers and raw body)
        body: Parsed request body, used to pick the PSTN or SIP scheme

    Returns:
        The authenticated source: "sip" or "pstn"

    Raises:
        WebhookAuthError: If the request is not authenticated or the
            required secret is not configured
    """
    headers = _normalize_headers(event.get("headers"))

    if body.get("source") == "sip":
        verify_sip_secret(headers)
        return "sip"

    verify_daily_signature(headers, _raw_body(event))
    return "pstn"


def verify_daily_signature(
    headers: dict[str, str], raw_body: bytes, now: float | None = None
) -> None:
    """Verify Daily's X-Pinless-Signature HMAC and reject stale timestamps."""
    timestamp = headers.get(DAILY_TIMESTAMP_HEADER, "")
    signature = headers.get(DAILY_SIGNATURE_HEADER, "")
    if not timestamp or not signature:
        raise WebhookAuthError(401, "missing_daily_signature")

    _check_timestamp(timestamp, time.time() if now is None else now)

    secret = _get_secret(DAILY_HMAC_SECRET_KEY)
    try:
        key = base64.b64decode(secret, validate=True)
    except (binascii.Error, ValueError):
        raise WebhookAuthError(
            500,
            "daily_hmac_secret_invalid",
            f"{DAILY_HMAC_SECRET_KEY} is not valid base64; copy the 'hmac' "
            "value from your Daily pinless_dialin config",
        )

    signed_payload = timestamp.encode("utf-8") + b"." + raw_body
    expected = base64.b64encode(hmac.new(key, signed_payload, hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature.encode("utf-8")):
        raise WebhookAuthError(401, "invalid_daily_signature")


def verify_sip_secret(headers: dict[str, str]) -> None:
    """Verify the shared secret presented by SIP clients."""
    provided = headers.get(SIP_SECRET_HEADER, "")
    if not provided:
        raise WebhookAuthError(401, "missing_sip_secret")

    expected = _get_secret(SIP_WEBHOOK_SECRET_KEY)
    if not hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8")):
        raise WebhookAuthError(401, "invalid_sip_secret")


def _check_timestamp(timestamp: str, now: float) -> None:
    """Reject non-numeric timestamps and those outside the tolerance window."""
    try:
        value = float(timestamp.strip())
    except ValueError:
        raise WebhookAuthError(401, "invalid_daily_timestamp")

    if not math.isfinite(value):
        raise WebhookAuthError(401, "invalid_daily_timestamp")

    if value > _EPOCH_MILLIS_THRESHOLD:
        value = value / 1000

    if abs(now - value) > TIMESTAMP_TOLERANCE_SECONDS:
        raise WebhookAuthError(401, "stale_daily_timestamp")


def _get_secret(key: str) -> str:
    """Return one configured webhook secret value, rejecting placeholders."""
    value = _load_webhook_secrets().get(key)
    if not isinstance(value, str) or not value or value == _PLACEHOLDER_VALUE:
        raise WebhookAuthError(
            500,
            f"{key.lower()}_not_configured",
            f"Set {key} in the webhook secret (WEBHOOK_SECRET_ARN); "
            "see docs/reference/daily-setup.md",
        )
    return value


def _load_webhook_secrets() -> dict:
    """Fetch the webhook secret JSON from Secrets Manager, cached per container."""
    global _secrets_client, _cached_secrets, _cache_expires_at

    now = time.monotonic()
    if _cached_secrets is not None and now < _cache_expires_at:
        return _cached_secrets

    secret_arn = os.environ.get("WEBHOOK_SECRET_ARN")
    if not secret_arn:
        raise WebhookAuthError(
            500, "webhook_secret_arn_not_set", "WEBHOOK_SECRET_ARN env var is not set"
        )

    try:
        if _secrets_client is None:
            _secrets_client = boto3.client("secretsmanager")
        response = _secrets_client.get_secret_value(SecretId=secret_arn)
        secrets = json.loads(response.get("SecretString") or "{}")
    except (BotoCoreError, ClientError, json.JSONDecodeError) as e:
        raise WebhookAuthError(500, "webhook_secret_unavailable", str(e))

    if not isinstance(secrets, dict):
        raise WebhookAuthError(
            500, "webhook_secret_invalid", "Webhook secret must be a JSON object"
        )

    _cached_secrets = secrets
    _cache_expires_at = now + SECRET_CACHE_TTL_SECONDS
    return secrets


def _normalize_headers(headers: dict | None) -> dict[str, str]:
    """Lower-case header names; API Gateway preserves the client's casing."""
    if not headers:
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items() if v is not None}


def _raw_body(event: dict) -> bytes:
    """Return the request body exactly as received, which is what Daily signs."""
    body = event.get("body")
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode("utf-8")
    # Direct (non-API Gateway) invocations may pass an already-parsed body;
    # compact separators match Daily's JSON.stringify serialization.
    return json.dumps(body, separators=(",", ":")).encode("utf-8")
