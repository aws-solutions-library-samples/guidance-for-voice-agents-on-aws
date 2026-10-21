"""
Bot Runner Lambda Handler

Handles Daily dial-in webhooks and routes to ECS voice service.

Architecture:
- Daily webhook triggers this Lambda
- Lambda authenticates the request (see webhook_auth.py)
- Lambda creates Daily room and generates tokens
- Lambda calls always-on ECS service with room config
- ECS service runs pipecat and connects to Daily room
- PSTN caller is routed to room via SIP
"""

import json
import logging
import os
import time
import uuid
from typing import Any

from daily_client import DailyClient
from service_client import EcsServiceClient
from webhook_auth import WebhookAuthError, authenticate_request

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def start_session(event: dict, context: Any) -> dict:
    """
    Handle Daily dial-in webhook and spawn voice session.

    Supports two modes:
    1. PSTN via Daily webhook: Has callId, callDomain, from fields.
       Must carry a valid Daily X-Pinless-Signature HMAC.
    2. SIP via direct request: Has source='sip', caller_id fields.
       Must carry the shared secret in the X-Sip-Webhook-Secret header.

    Requests are authenticated before any room or session is created.

    Expected PSTN webhook payload:
    {
        "callId": "string",
        "callDomain": "string",
        "from": "+15551234567",
        "to": "+15559876543",
        "direction": "inbound"
    }

    Expected SIP request payload:
    {
        "source": "sip",
        "caller_id": "web-client-001",
        "caller_number": "sip:100@asterisk.local"
    }

    Returns:
        API Gateway response with session details or error
    """
    request_id = context.aws_request_id if context else str(uuid.uuid4())
    logger.info(f"[{request_id}] Received webhook event")

    try:
        # Parse request body
        body = _parse_body(event)

        # Authenticate before doing any billable work
        try:
            source = authenticate_request(event, body)
        except WebhookAuthError as e:
            return _auth_error_response(e, request_id)

        logger.info(f"[{request_id}] Authenticated {source} request: {body}")

        call_id = body.get("callId")
        call_domain = body.get("callDomain")

        # Handle SIP requests differently
        if source == "sip":
            logger.info(f"[{request_id}] Detected SIP request")
            return _handle_sip_request(body, request_id)

        # Daily sends a signed {"test": "test"} body whenever pinless_dialin
        # is configured; acknowledge it without starting a session
        if body.get("test") == "test" and not call_id and not call_domain:
            logger.info(f"[{request_id}] Daily webhook test request acknowledged")
            return _success_response(200, {"status": "ok"})

        # PSTN flow - validate required fields
        from_number = body.get("from", "unknown")

        if not call_id:
            return _error_response(400, "Missing required field: callId")
        if not call_domain:
            return _error_response(400, "Missing required field: callDomain")

        # Initialize clients
        daily_client = DailyClient()
        service_client = EcsServiceClient()

        # Generate unique session ID
        session_id = f"voice-{call_id}-{uuid.uuid4().hex[:8]}"
        logger.info(f"[{request_id}] Created session_id: {session_id}")

        # Step 1: Create Daily room with SIP enabled
        logger.info(f"[{request_id}] Creating Daily room")
        room = daily_client.create_room(
            name=f"voice-{call_id}",
            properties={
                "enable_chat": False,
                "enable_screenshare": False,
                "enable_recording": False,
                "enable_transcription": False,
                "sip": {
                    "display_name": "Voice Assistant",
                    "video": False,
                    "sip_mode": "dial-in",
                },
                "exp": int(time.time()) + 3600,  # 1 hour from now
            },
        )
        room_url = room["url"]
        room_name = room["name"]
        logger.info(f"[{request_id}] Created room: {room_name}")

        # Step 2: Generate meeting token for the bot
        logger.info(f"[{request_id}] Generating bot token")
        bot_token = daily_client.create_meeting_token(
            room_name=room_name,
            properties={
                "is_owner": True,
                "user_name": "Voice Assistant",
                "enable_screenshare": False,
                "start_video_off": True,
                "start_audio_off": False,
                "exp": int(time.time()) + 3600,  # 1 hour from now
            },
        )
        logger.info(f"[{request_id}] Generated bot token")

        # Step 3: Get SIP URI for call routing
        sip_uri = daily_client.get_sip_uri(room_name)
        logger.info(f"[{request_id}] SIP URI: {sip_uri}")

        # Step 4: Call the always-on ECS service to handle the call
        # The service runs an HTTP server that accepts call requests
        logger.info(f"[{request_id}] Calling ECS service")
        service_response = service_client.start_call(
            room_url=room_url,
            room_token=bot_token,
            session_id=session_id,
            system_prompt=_get_system_prompt(from_number),
            dialin_settings={
                "call_id": call_id,
                "call_domain": call_domain,
                "sip_uri": sip_uri,
            },
        )
        logger.info(
            f"[{request_id}] Service response: {service_response.get('status')}"
        )

        # Check if voice agent accepted the call
        if service_response.get("status") not in ("started",):
            logger.error(
                f"[{request_id}] Voice agent rejected call: {service_response}"
            )
            return _error_response(
                503,
                f"Voice agent unavailable: {service_response.get('error', 'unknown')}",
            )

        # Step 5: Return SIP transfer response to Daily
        # Daily expects a sipUri field to route the call
        response_body = {
            "sessionId": session_id,
            "roomUrl": room_url,
            "sipUri": sip_uri,
            "status": service_response.get("status", "started"),
            "message": "Voice session started successfully",
        }

        logger.info(f"[{request_id}] Session started successfully")
        return _success_response(200, response_body)

    except ValueError as e:
        logger.error(f"[{request_id}] Validation error: {e}")
        return _error_response(400, str(e))
    except Exception as e:
        logger.exception(f"[{request_id}] Unexpected error: {e}")
        return _error_response(500, "Internal server error")


def _handle_sip_request(body: dict, request_id: str) -> dict:
    """
    Handle SIP-initiated voice session.

    Creates a Daily room and starts bot without pinless dial-in configuration.
    Returns SIP URI for Asterisk to dial.
    """
    try:
        caller_id = body.get("caller_id", "unknown")
        caller_number = body.get("caller_number", "unknown")

        logger.info(f"[{request_id}] Processing SIP request from: {caller_id}")

        # Initialize clients
        daily_client = DailyClient()
        service_client = EcsServiceClient()

        # Generate unique session ID for SIP
        session_id = f"sip-{uuid.uuid4().hex[:8]}"
        logger.info(f"[{request_id}] Created session_id: {session_id}")

        # Step 1: Create Daily room with SIP enabled (no pinless dial-in)
        logger.info(f"[{request_id}] Creating Daily room for SIP")
        room = daily_client.create_room(
            name=f"sip-{session_id}",
            properties={
                "enable_chat": False,
                "enable_screenshare": False,
                "enable_recording": False,
                "enable_transcription": False,
                "sip": {
                    "display_name": "Voice Assistant",
                    "video": False,
                    "sip_mode": "dial-in",
                },
                "exp": int(time.time()) + 3600,  # 1 hour from now
            },
        )
        room_url = room["url"]
        room_name = room["name"]
        logger.info(f"[{request_id}] Created room: {room_name}")

        # Step 2: Generate meeting token for the bot
        logger.info(f"[{request_id}] Generating bot token")
        bot_token = daily_client.create_meeting_token(
            room_name=room_name,
            properties={
                "is_owner": True,
                "user_name": "Voice Assistant",
                "enable_screenshare": False,
                "start_video_off": True,
                "start_audio_off": False,
                "exp": int(time.time()) + 3600,  # 1 hour from now
            },
        )
        logger.info(f"[{request_id}] Generated bot token")

        # Step 3: Get SIP URI for call routing
        sip_uri = daily_client.get_sip_uri(room_name)
        logger.info(f"[{request_id}] SIP URI: {sip_uri}")

        # Step 4: Call the always-on ECS service to handle the call
        # For SIP calls, we don't pass dialin_settings (no pinless dial-in)
        logger.info(f"[{request_id}] Calling ECS service")
        service_response = service_client.start_call(
            room_url=room_url,
            room_token=bot_token,
            session_id=session_id,
            system_prompt=_get_system_prompt(caller_number),
            # dialin_settings is None for SIP calls
        )
        logger.info(
            f"[{request_id}] Service response: {service_response.get('status')}"
        )

        # Check if voice agent accepted the call
        if service_response.get("status") not in ("started",):
            logger.error(
                f"[{request_id}] Voice agent rejected call: {service_response}"
            )
            return _error_response(
                503,
                f"Voice agent unavailable: {service_response.get('error', 'unknown')}",
            )

        # Step 5: Return response with SIP URI
        # Asterisk will use this SIP URI to dial the room
        response_body = {
            "sessionId": session_id,
            "roomUrl": room_url,
            "sipUri": sip_uri,
            "status": service_response.get("status", "started"),
            "message": "Voice session started successfully",
        }

        logger.info(f"[{request_id}] SIP session started successfully")
        return _success_response(200, response_body)

    except ValueError as e:
        logger.error(f"[{request_id}] SIP validation error: {e}")
        return _error_response(400, str(e))
    except Exception as e:
        logger.exception(f"[{request_id}] SIP unexpected error: {e}")
        return _error_response(500, "Internal server error")


def _parse_body(event: dict) -> dict:
    """Parse request body from API Gateway event."""
    body = event.get("body", "{}")

    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON body: {e}")

    return body if isinstance(body, dict) else {}


def _auth_error_response(error: WebhookAuthError, request_id: str) -> dict:
    """Log an authentication failure and build the rejection response."""
    if error.status_code >= 500:
        # Misconfiguration: the request may be legitimate but cannot be verified
        logger.error(f"[{request_id}] Webhook rejected: {error.reason}. {error.detail}")
        return _error_response(500, "Webhook authentication unavailable")

    logger.warning(f"[{request_id}] Webhook rejected: {error.reason}")
    return _error_response(401, "Unauthorized")


def _get_system_prompt(caller_id: str) -> str:
    """
    Generate system prompt for the voice assistant.

    Can be customized based on caller ID, time of day, etc.
    """
    return """You are a helpful voice assistant powered by Claude.

Your role is to have natural, conversational interactions with callers.
Be concise but friendly - remember this is a phone call, not a text chat.

Guidelines:
- Keep responses brief and conversational (1-3 sentences typically)
- Use natural speech patterns, not formal writing
- Ask clarifying questions when needed
- Be helpful and patient

Tool Usage:
- When using tools, call them directly without explaining what you're doing first
- After the tool returns, respond naturally with the result
- Do NOT say "Let me..." or "I'll use..." before calling a tool

The caller is reaching you via phone. Greet them warmly and ask how you can help."""


def _success_response(status_code: int, body: dict) -> dict:
    """Create successful API Gateway response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _error_response(status_code: int, message: str) -> dict:
    """Create error API Gateway response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(
            {
                "error": message,
                "status": "error",
            }
        ),
    }
