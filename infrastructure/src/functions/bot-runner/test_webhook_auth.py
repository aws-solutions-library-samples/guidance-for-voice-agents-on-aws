"""
Unit tests for webhook authentication.

Run with: pytest test_webhook_auth.py -v
"""

import base64
import hashlib
import hmac
import json
import unittest
from unittest.mock import MagicMock, patch

import webhook_auth
from botocore.exceptions import ClientError
from webhook_auth import WebhookAuthError

# Reference vector produced by running the JavaScript snippet from Daily's docs
# (https://docs.daily.co/guides/products/dial-in-dial-out/dialin-pinless) with
# the example secret shown there.
DOCS_HMAC_SECRET = "NQrSA5z0FkJ44QPrFerW7uCc5kdNLv3l2FDEKDanL1U="
DOCS_TIMESTAMP = "1792610000"
DOCS_BODY = (
    b'{"From":"+15551234567","To":"+15559876543",'
    b'"callId":"a7bf8461-7d33-4083-9677-5cfdb4f337a5",'
    b'"callDomain":"9c975848-a067-4de0-bf47-1b4179ceba4e"}'
)
DOCS_SIGNATURE = "k4RGdZc6Q8IhA4vC0WxpW0VZOh+ukeiW+5il2ZD/oBU="
DOCS_NOW = float(DOCS_TIMESTAMP)

SECRETS = {"DAILY_HMAC_SECRET": DOCS_HMAC_SECRET, "SIP_WEBHOOK_SECRET": "sip-secret"}


def _headers(timestamp: str = DOCS_TIMESTAMP, signature: str = DOCS_SIGNATURE) -> dict:
    return {
        webhook_auth.DAILY_TIMESTAMP_HEADER: timestamp,
        webhook_auth.DAILY_SIGNATURE_HEADER: signature,
    }


@patch("webhook_auth._load_webhook_secrets", return_value=SECRETS)
class TestVerifyDailySignature(unittest.TestCase):
    """Tests for verify_daily_signature."""

    def test_matches_daily_reference_implementation(self, _):
        webhook_auth.verify_daily_signature(_headers(), DOCS_BODY, now=DOCS_NOW)

    def test_accepts_millisecond_timestamp(self, _):
        # Daily's docs do not state units; a millisecond timestamp signed the
        # same way must also verify.
        timestamp_ms = DOCS_TIMESTAMP + "000"
        signature = _sign(DOCS_HMAC_SECRET, timestamp_ms, DOCS_BODY)

        webhook_auth.verify_daily_signature(
            _headers(timestamp_ms, signature), DOCS_BODY, now=DOCS_NOW
        )

    def test_accepts_timestamp_within_tolerance(self, _):
        now = DOCS_NOW + webhook_auth.TIMESTAMP_TOLERANCE_SECONDS
        webhook_auth.verify_daily_signature(_headers(), DOCS_BODY, now=now)

    def test_rejects_stale_timestamp(self, _):
        now = DOCS_NOW + webhook_auth.TIMESTAMP_TOLERANCE_SECONDS + 1
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth.verify_daily_signature(_headers(), DOCS_BODY, now=now)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.reason, "stale_daily_timestamp")

    def test_rejects_future_timestamp(self, _):
        now = DOCS_NOW - webhook_auth.TIMESTAMP_TOLERANCE_SECONDS - 1
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth.verify_daily_signature(_headers(), DOCS_BODY, now=now)
        self.assertEqual(ctx.exception.reason, "stale_daily_timestamp")

    def test_rejects_non_numeric_timestamp(self, _):
        for timestamp in ("not-a-time", "nan", "inf"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(WebhookAuthError) as ctx:
                    webhook_auth.verify_daily_signature(
                        _headers(timestamp=timestamp), DOCS_BODY, now=DOCS_NOW
                    )
                self.assertEqual(ctx.exception.reason, "invalid_daily_timestamp")

    def test_rejects_hex_encoded_signature(self, _):
        # Daily uses base64 digests; a hex digest of the same HMAC must fail.
        hex_signature = base64.b64decode(DOCS_SIGNATURE).hex()
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth.verify_daily_signature(
                _headers(signature=hex_signature), DOCS_BODY, now=DOCS_NOW
            )
        self.assertEqual(ctx.exception.reason, "invalid_daily_signature")

    def test_rejects_non_ascii_signature(self, _):
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth.verify_daily_signature(
                _headers(signature="sïgnature"), DOCS_BODY, now=DOCS_NOW
            )
        self.assertEqual(ctx.exception.reason, "invalid_daily_signature")

    def test_missing_headers_do_not_load_secret(self, mock_load):
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth.verify_daily_signature({}, DOCS_BODY, now=DOCS_NOW)
        self.assertEqual(ctx.exception.reason, "missing_daily_signature")
        mock_load.assert_not_called()

    def test_invalid_base64_secret_is_misconfiguration(self, mock_load):
        mock_load.return_value = {"DAILY_HMAC_SECRET": "not base64!"}
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth.verify_daily_signature(_headers(), DOCS_BODY, now=DOCS_NOW)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.reason, "daily_hmac_secret_invalid")


@patch("webhook_auth._load_webhook_secrets", return_value=SECRETS)
class TestVerifySipSecret(unittest.TestCase):
    """Tests for verify_sip_secret."""

    def test_accepts_matching_secret(self, _):
        webhook_auth.verify_sip_secret({webhook_auth.SIP_SECRET_HEADER: "sip-secret"})

    def test_rejects_wrong_secret(self, _):
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth.verify_sip_secret({webhook_auth.SIP_SECRET_HEADER: "nope"})
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.reason, "invalid_sip_secret")

    def test_rejects_missing_secret(self, _):
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth.verify_sip_secret({})
        self.assertEqual(ctx.exception.reason, "missing_sip_secret")

    def test_unconfigured_secret_is_misconfiguration(self, mock_load):
        for secrets in ({}, {"SIP_WEBHOOK_SECRET": ""}):
            with self.subTest(secrets=secrets):
                mock_load.return_value = secrets
                with self.assertRaises(WebhookAuthError) as ctx:
                    webhook_auth.verify_sip_secret(
                        {webhook_auth.SIP_SECRET_HEADER: "sip-secret"}
                    )
                self.assertEqual(ctx.exception.status_code, 500)
                self.assertEqual(
                    ctx.exception.reason, "sip_webhook_secret_not_configured"
                )


class TestAuthenticateRequest(unittest.TestCase):
    """Tests for authenticate_request scheme selection."""

    @patch("webhook_auth.verify_sip_secret")
    @patch("webhook_auth.verify_daily_signature")
    def test_sip_source_uses_shared_secret(self, mock_daily, mock_sip):
        event = {"headers": {"X-Sip-Webhook-Secret": "s"}, "body": "{}"}
        self.assertEqual(
            webhook_auth.authenticate_request(event, {"source": "sip"}), "sip"
        )
        mock_sip.assert_called_once_with({"x-sip-webhook-secret": "s"})
        mock_daily.assert_not_called()

    @patch("webhook_auth.verify_sip_secret")
    @patch("webhook_auth.verify_daily_signature")
    def test_other_requests_use_daily_signature_over_raw_body(
        self, mock_daily, mock_sip
    ):
        raw = '{"callId": "c",  "callDomain": "d"}'
        event = {"headers": {"X-Pinless-Signature": "sig"}, "body": raw}
        self.assertEqual(
            webhook_auth.authenticate_request(event, json.loads(raw)), "pstn"
        )
        mock_daily.assert_called_once_with(
            {"x-pinless-signature": "sig"}, raw.encode("utf-8")
        )
        mock_sip.assert_not_called()


class TestLoadWebhookSecrets(unittest.TestCase):
    """Tests for Secrets Manager loading and per-container caching."""

    ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:webhook"

    def setUp(self):
        self._reset_cache()
        self.addCleanup(self._reset_cache)
        self.client = MagicMock()
        self.client.get_secret_value.return_value = {
            "SecretString": json.dumps(SECRETS)
        }
        patcher = patch("webhook_auth.boto3.client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _reset_cache():
        webhook_auth._secrets_client = None
        webhook_auth._cached_secrets = None
        webhook_auth._cache_expires_at = 0.0

    @patch.dict("os.environ", {"WEBHOOK_SECRET_ARN": ARN})
    def test_caches_secret_within_ttl(self):
        self.assertEqual(webhook_auth._load_webhook_secrets(), SECRETS)
        self.assertEqual(webhook_auth._load_webhook_secrets(), SECRETS)
        self.client.get_secret_value.assert_called_once_with(SecretId=self.ARN)

    @patch.dict("os.environ", {"WEBHOOK_SECRET_ARN": ARN})
    def test_refetches_after_ttl(self):
        with patch("webhook_auth.time.monotonic", return_value=1000.0):
            webhook_auth._load_webhook_secrets()
        expired = 1000.0 + webhook_auth.SECRET_CACHE_TTL_SECONDS
        with patch("webhook_auth.time.monotonic", return_value=expired):
            webhook_auth._load_webhook_secrets()
        self.assertEqual(self.client.get_secret_value.call_count, 2)

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_arn_is_misconfiguration(self):
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth._load_webhook_secrets()
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.reason, "webhook_secret_arn_not_set")

    @patch.dict("os.environ", {"WEBHOOK_SECRET_ARN": ARN})
    def test_fetch_failure_is_not_cached(self):
        self.client.get_secret_value.side_effect = [
            ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue"),
            {"SecretString": json.dumps(SECRETS)},
        ]
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth._load_webhook_secrets()
        self.assertEqual(ctx.exception.reason, "webhook_secret_unavailable")
        self.assertEqual(webhook_auth._load_webhook_secrets(), SECRETS)

    @patch.dict("os.environ", {"WEBHOOK_SECRET_ARN": ARN})
    def test_non_object_secret_is_misconfiguration(self):
        self.client.get_secret_value.return_value = {"SecretString": '"just-a-string"'}
        with self.assertRaises(WebhookAuthError) as ctx:
            webhook_auth._load_webhook_secrets()
        self.assertEqual(ctx.exception.reason, "webhook_secret_invalid")


def _sign(secret_b64: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        base64.b64decode(secret_b64), timestamp.encode() + b"." + body, hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode()


if __name__ == "__main__":
    unittest.main()
