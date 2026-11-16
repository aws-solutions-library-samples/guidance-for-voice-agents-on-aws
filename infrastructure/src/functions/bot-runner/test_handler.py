"""
Unit tests for Bot Runner Lambda handler.

Run with: pytest test_handler.py -v
"""

import base64
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

# Set environment variables before importing handler
os.environ["DAILY_API_KEY_SECRET_ARN"] = (
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret"
)
os.environ["WEBHOOK_SECRET_ARN"] = (
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-webhook-secret"
)

import handler

TEST_HMAC_SECRET = base64.b64encode(b"test-daily-hmac-secret-bytes").decode()
TEST_SIP_SECRET = "test-sip-shared-secret"
TEST_WEBHOOK_SECRETS = {
    "DAILY_HMAC_SECRET": TEST_HMAC_SECRET,
    "SIP_WEBHOOK_SECRET": TEST_SIP_SECRET,
}


def sign_daily(raw_body: str, timestamp: str, secret: str = TEST_HMAC_SECRET) -> str:
    """Compute Daily's X-Pinless-Signature for a raw body."""
    digest = hmac.new(
        base64.b64decode(secret),
        f"{timestamp}.{raw_body}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode()


class WebhookSecretsTestCase(unittest.TestCase):
    """Base class that stubs the webhook secret instead of calling AWS."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_context = MagicMock()
        self.mock_context.aws_request_id = "test-request-id"
        patcher = patch(
            "webhook_auth._load_webhook_secrets",
            return_value=dict(TEST_WEBHOOK_SECRETS),
        )
        self.mock_load_secrets = patcher.start()
        self.addCleanup(patcher.stop)

    def _make_event(
        self, body: dict, timestamp: str | None = None, signature: str | None = None
    ) -> dict:
        """Create a Daily-signed API Gateway event with body."""
        raw_body = json.dumps(body)
        timestamp = timestamp or str(int(time.time()))
        return {
            "body": raw_body,
            "headers": {
                "Content-Type": "application/json",
                "X-Pinless-Timestamp": timestamp,
                "X-Pinless-Signature": signature or sign_daily(raw_body, timestamp),
            },
            "httpMethod": "POST",
            "path": "/start",
        }

    def _make_sip_event(self, body: dict, secret: str | None = TEST_SIP_SECRET) -> dict:
        """Create a SIP API Gateway event, optionally carrying the shared secret."""
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers["X-Sip-Webhook-Secret"] = secret
        return {
            "body": json.dumps(body),
            "headers": headers,
            "httpMethod": "POST",
            "path": "/start",
        }


class TestStartSession(WebhookSecretsTestCase):
    """Tests for start_session handler."""

    def test_missing_call_id_returns_400(self):
        """Should return 400 when callId is missing."""
        event = self._make_event(
            {
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertIn("callId", body["error"])

    def test_missing_call_domain_returns_400(self):
        """Should return 400 when callDomain is missing."""
        event = self._make_event(
            {
                "callId": "test-call-123",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertIn("callDomain", body["error"])

    def test_invalid_json_returns_400(self):
        """Should return 400 for invalid JSON body."""
        event = {
            "body": "not valid json {{{",
            "headers": {"Content-Type": "application/json"},
        }

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertIn("Invalid JSON", body["error"])

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_successful_session_start(self, mock_daily_cls, mock_service_cls):
        """Should return 200 and session details on success."""
        # Set up mocks
        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {
            "url": "https://test.daily.co/voice-test-123",
            "name": "voice-test-123",
            "id": "room-id-abc",
        }
        mock_daily.create_meeting_token.return_value = "test-token-xyz"
        mock_daily.get_sip_uri.return_value = "sip:room-id-abc@sip.daily.co"
        mock_daily_cls.return_value = mock_daily

        mock_service = MagicMock()
        mock_service.start_call.return_value = {"status": "started"}
        mock_service_cls.return_value = mock_service

        event = self._make_event(
            {
                "callId": "test-call-123",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "started")
        self.assertIn("sessionId", body)
        self.assertIn("roomUrl", body)
        self.assertIn("sipUri", body)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_daily_api_error_returns_500(self, mock_daily_cls, mock_service_cls):
        """Should return 500 when Daily API fails."""
        mock_daily = MagicMock()
        mock_daily.create_room.side_effect = ValueError("Daily API error: 401")
        mock_daily_cls.return_value = mock_daily

        event = self._make_event(
            {
                "callId": "test-call-123",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 500)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_ecs_service_error_returns_500(self, mock_daily_cls, mock_service_cls):
        """Should return 500 when ECS service fails."""
        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {
            "url": "https://test.daily.co/voice-test-123",
            "name": "voice-test-123",
            "id": "room-id-abc",
        }
        mock_daily.create_meeting_token.return_value = "test-token-xyz"
        mock_daily.get_sip_uri.return_value = "sip:room-id-abc@sip.daily.co"
        mock_daily_cls.return_value = mock_daily

        mock_service = MagicMock()
        mock_service.start_call.side_effect = Exception("ECS service error")
        mock_service_cls.return_value = mock_service

        event = self._make_event(
            {
                "callId": "test-call-123",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 500)

    def test_empty_body_returns_400(self):
        """Should return 400 when a signed body is empty."""
        event = self._make_event({})

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 400)

    def test_unsigned_empty_body_returns_401(self):
        """An empty body without a signature must not fall through to SIP."""
        event = {"body": "{}"}

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 401)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_session_id_format(self, mock_daily_cls, mock_service_cls):
        """Session ID should include call ID prefix."""
        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {
            "url": "https://test.daily.co/voice-test-123",
            "name": "voice-test-123",
            "id": "room-id-abc",
        }
        mock_daily.create_meeting_token.return_value = "test-token-xyz"
        mock_daily.get_sip_uri.return_value = "sip:room-id-abc@sip.daily.co"
        mock_daily_cls.return_value = mock_daily

        mock_service = MagicMock()
        mock_service.start_call.return_value = {"status": "started"}
        mock_service_cls.return_value = mock_service

        event = self._make_event(
            {
                "callId": "my-call-id",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)
        body = json.loads(response["body"])

        self.assertTrue(body["sessionId"].startswith("voice-my-call-id-"))


class TestWebhookAuthentication(WebhookSecretsTestCase):
    """Requests must be authenticated before any room or session is created."""

    PSTN_BODY = {
        "callId": "test-call-123",
        "callDomain": "test.daily.co",
        "From": "+15551234567",
        "To": "+15559876543",
    }
    SIP_BODY = {
        "source": "sip",
        "caller_id": "web-client-001",
        "caller_number": "sip:100@asterisk.local",
    }

    def _mock_clients(self, mock_daily_cls, mock_service_cls):
        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {
            "url": "https://test.daily.co/voice-test-123",
            "name": "voice-test-123",
        }
        mock_daily.create_meeting_token.return_value = "test-token-xyz"
        mock_daily.get_sip_uri.return_value = "sip:room@sip.daily.co"
        mock_daily_cls.return_value = mock_daily
        mock_service_cls.return_value.start_call.return_value = {"status": "started"}

    def _assert_rejected(self, response, mock_daily_cls, mock_service_cls, status=401):
        self.assertEqual(response["statusCode"], status)
        self.assertEqual(json.loads(response["body"])["status"], "error")
        mock_daily_cls.assert_not_called()
        mock_service_cls.assert_not_called()

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_valid_daily_signature_starts_session(
        self, mock_daily_cls, mock_service_cls
    ):
        self._mock_clients(mock_daily_cls, mock_service_cls)

        response = handler.start_session(
            self._make_event(self.PSTN_BODY), self.mock_context
        )

        self.assertEqual(response["statusCode"], 200)
        mock_service_cls.return_value.start_call.assert_called_once()

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_lowercase_signature_headers_accepted(
        self, mock_daily_cls, mock_service_cls
    ):
        self._mock_clients(mock_daily_cls, mock_service_cls)
        event = self._make_event(self.PSTN_BODY)
        event["headers"] = {k.lower(): v for k, v in event["headers"].items()}

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 200)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_missing_signature_returns_401(self, mock_daily_cls, mock_service_cls):
        event = self._make_event(self.PSTN_BODY)
        del event["headers"]["X-Pinless-Signature"]

        response = handler.start_session(event, self.mock_context)

        self._assert_rejected(response, mock_daily_cls, mock_service_cls)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_missing_timestamp_returns_401(self, mock_daily_cls, mock_service_cls):
        event = self._make_event(self.PSTN_BODY)
        del event["headers"]["X-Pinless-Timestamp"]

        response = handler.start_session(event, self.mock_context)

        self._assert_rejected(response, mock_daily_cls, mock_service_cls)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_no_headers_returns_401(self, mock_daily_cls, mock_service_cls):
        event = {"body": json.dumps(self.PSTN_BODY), "headers": None}

        response = handler.start_session(event, self.mock_context)

        self._assert_rejected(response, mock_daily_cls, mock_service_cls)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_invalid_signature_returns_401(self, mock_daily_cls, mock_service_cls):
        timestamp = str(int(time.time()))
        wrong_secret = base64.b64encode(b"some-other-secret").decode()
        event = self._make_event(
            self.PSTN_BODY,
            timestamp=timestamp,
            signature=sign_daily(json.dumps(self.PSTN_BODY), timestamp, wrong_secret),
        )

        response = handler.start_session(event, self.mock_context)

        self._assert_rejected(response, mock_daily_cls, mock_service_cls)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_tampered_body_returns_401(self, mock_daily_cls, mock_service_cls):
        event = self._make_event(self.PSTN_BODY)
        event["body"] = json.dumps({**self.PSTN_BODY, "callId": "attacker-call"})

        response = handler.start_session(event, self.mock_context)

        self._assert_rejected(response, mock_daily_cls, mock_service_cls)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_replayed_signature_returns_401(self, mock_daily_cls, mock_service_cls):
        """A validly signed request captured earlier is rejected once stale."""
        captured = self._make_event(
            self.PSTN_BODY, timestamp=str(int(time.time()) - 301)
        )

        response = handler.start_session(captured, self.mock_context)

        self._assert_rejected(response, mock_daily_cls, mock_service_cls)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_replay_with_refreshed_timestamp_returns_401(
        self, mock_daily_cls, mock_service_cls
    ):
        """Swapping in a fresh timestamp invalidates the captured signature."""
        captured = self._make_event(
            self.PSTN_BODY, timestamp=str(int(time.time()) - 3600)
        )
        captured["headers"]["X-Pinless-Timestamp"] = str(int(time.time()))

        response = handler.start_session(captured, self.mock_context)

        self._assert_rejected(response, mock_daily_cls, mock_service_cls)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_daily_test_request_acknowledged_without_session(
        self, mock_daily_cls, mock_service_cls
    ):
        response = handler.start_session(
            self._make_event({"test": "test"}), self.mock_context
        )

        self.assertEqual(response["statusCode"], 200)
        mock_daily_cls.assert_not_called()
        mock_service_cls.assert_not_called()

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_unconfigured_hmac_secret_rejects_with_500(
        self, mock_daily_cls, mock_service_cls
    ):
        self.mock_load_secrets.return_value = {
            "DAILY_HMAC_SECRET": "PLACEHOLDER_REPLACE_AFTER_DEPLOY",
            "SIP_WEBHOOK_SECRET": TEST_SIP_SECRET,
        }

        with self.assertLogs(level="ERROR") as logs:
            response = handler.start_session(
                self._make_event(self.PSTN_BODY), self.mock_context
            )

        self._assert_rejected(response, mock_daily_cls, mock_service_cls, status=500)
        self.assertIn("daily_hmac_secret_not_configured", "\n".join(logs.output))

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_valid_sip_secret_starts_session(self, mock_daily_cls, mock_service_cls):
        self._mock_clients(mock_daily_cls, mock_service_cls)

        response = handler.start_session(
            self._make_sip_event(self.SIP_BODY), self.mock_context
        )

        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(json.loads(response["body"])["sessionId"].startswith("sip-"))

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_missing_sip_secret_returns_401(self, mock_daily_cls, mock_service_cls):
        response = handler.start_session(
            self._make_sip_event(self.SIP_BODY, secret=None), self.mock_context
        )

        self._assert_rejected(response, mock_daily_cls, mock_service_cls)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_invalid_sip_secret_returns_401(self, mock_daily_cls, mock_service_cls):
        response = handler.start_session(
            self._make_sip_event(self.SIP_BODY, secret="wrong-secret"),
            self.mock_context,
        )

        self._assert_rejected(response, mock_daily_cls, mock_service_cls)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_sip_request_with_daily_signature_only_returns_401(
        self, mock_daily_cls, mock_service_cls
    ):
        """source=sip always requires the SIP shared secret."""
        response = handler.start_session(
            self._make_event(self.SIP_BODY), self.mock_context
        )

        self._assert_rejected(response, mock_daily_cls, mock_service_cls)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_unconfigured_sip_secret_rejects_with_500(
        self, mock_daily_cls, mock_service_cls
    ):
        self.mock_load_secrets.return_value = {"DAILY_HMAC_SECRET": TEST_HMAC_SECRET}

        response = handler.start_session(
            self._make_sip_event(self.SIP_BODY), self.mock_context
        )

        self._assert_rejected(response, mock_daily_cls, mock_service_cls, status=500)


class TestParseBody(unittest.TestCase):
    """Tests for _parse_body helper."""

    def test_parses_json_string(self):
        """Should parse JSON string body."""
        event = {"body": '{"key": "value"}'}
        result = handler._parse_body(event)
        self.assertEqual(result, {"key": "value"})

    def test_handles_dict_body(self):
        """Should handle dict body (already parsed)."""
        event = {"body": {"key": "value"}}
        result = handler._parse_body(event)
        self.assertEqual(result, {"key": "value"})

    def test_handles_empty_body(self):
        """Should return empty dict for missing body."""
        event = {}
        result = handler._parse_body(event)
        self.assertEqual(result, {})

    def test_raises_on_invalid_json(self):
        """Should raise ValueError for invalid JSON."""
        event = {"body": "not json"}
        with self.assertRaises(ValueError):
            handler._parse_body(event)


class TestResponseHelpers(unittest.TestCase):
    """Tests for response helper functions."""

    def test_success_response_format(self):
        """Success response should have correct format."""
        response = handler._success_response(200, {"key": "value"})

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "application/json")
        self.assertIn("Access-Control-Allow-Origin", response["headers"])
        body = json.loads(response["body"])
        self.assertEqual(body["key"], "value")

    def test_error_response_format(self):
        """Error response should have correct format."""
        response = handler._error_response(400, "Test error")

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(response["headers"]["Content-Type"], "application/json")
        body = json.loads(response["body"])
        self.assertEqual(body["error"], "Test error")
        self.assertEqual(body["status"], "error")


class TestGetSystemPrompt(unittest.TestCase):
    """Tests for _get_system_prompt helper."""

    def test_returns_non_empty_prompt(self):
        """Should return a non-empty system prompt."""
        prompt = handler._get_system_prompt("+15551234567")
        self.assertTrue(len(prompt) > 0)

    def test_prompt_contains_assistant_context(self):
        """Prompt should contain assistant context."""
        prompt = handler._get_system_prompt("+15551234567")
        self.assertIn("assistant", prompt.lower())

    def test_prompt_contains_tool_usage_guidance(self):
        """Prompt should contain tool usage guidance for clean context aggregation."""
        prompt = handler._get_system_prompt("+15551234567")
        # Tool usage guidance prevents LLM from outputting text before tool calls,
        # which would cause context aggregation issues with concurrent text/tool frames
        self.assertIn("tool", prompt.lower())
        self.assertIn("directly", prompt.lower())


if __name__ == "__main__":
    unittest.main()
