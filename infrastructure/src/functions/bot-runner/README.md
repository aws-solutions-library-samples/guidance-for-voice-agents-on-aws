# Bot Runner Lambda Function

Python Lambda function that handles Daily dial-in webhooks and routes calls to the ECS voice service.

## Architecture

```
Daily PSTN ──> API Gateway ──> Lambda ──> Daily API (create room)
    |                             |
    |                             └──> ECS Service (POST /call)
    |                                        |
    └────────────────────────────────────────┘
              (SIP transfer to room)
```

## Components

### handler.py
Main Lambda entry point with `/start` webhook handler:
- Parses Daily dial-in webhook payload
- Authenticates the request before any other work (see `webhook_auth.py`)
- Validates required fields (callId, callDomain)
- Creates Daily room with SIP enabled
- Generates bot meeting token
- Calls ECS voice service to handle the call
- Returns SIP URI for call routing
- Supports both PSTN (via Daily webhook) and SIP (via direct request) modes

### webhook_auth.py
Request authentication for `/start`:
- PSTN: verifies Daily's `X-Pinless-Signature` HMAC and rejects timestamps more than 5 minutes old or in the future
- SIP (`"source": "sip"`): requires the `X-Sip-Webhook-Secret` shared-secret header
- Constant-time comparisons
- Secrets loaded from Secrets Manager (`WEBHOOK_SECRET_ARN`), cached per container for 5 minutes

See [Webhook Authentication](../../../../docs/reference/daily-setup.md#webhook-authentication) for configuring `DAILY_HMAC_SECRET` and `SIP_WEBHOOK_SECRET`.

### daily_client.py
Daily.co REST API client:
- Room creation with SIP configuration
- Meeting token generation
- SIP URI retrieval
- API key fetched from Secrets Manager

### service_client.py
ECS Service HTTP client:
- Reads service endpoint from SSM Parameter Store
- Sends call requests (POST /call) with room config
- Health check support
- Fallback to environment variable for endpoint

### ecs_client.py
AWS ECS client for direct task management:
- Start ECS Fargate tasks with environment overrides
- Task status checking
- Task termination

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DAILY_API_KEY_SECRET_ARN` | ARN of secret containing Daily API key | Yes |
| `WEBHOOK_SECRET_ARN` | ARN of secret containing `DAILY_HMAC_SECRET` and `SIP_WEBHOOK_SECRET` | Yes |
| `ECS_SERVICE_ENDPOINT` | HTTP endpoint for ECS voice service (fallback) | No |
| `LOG_LEVEL` | Logging level (DEBUG, INFO, etc.) | No |

The ECS service endpoint is primarily read from SSM Parameter Store (`/voice-agent/ecs/service-endpoint`).

## API

### POST /start

Handle Daily dial-in webhook.

**PSTN Request (from Daily webhook):** signed by Daily with `X-Pinless-Timestamp` and `X-Pinless-Signature` headers.
```json
{
  "callId": "abc123",
  "callDomain": "your-domain.daily.co",
  "from": "+15551234567",
  "to": "+15559876543",
  "direction": "inbound"
}
```

**SIP Request (direct):** must include the `X-Sip-Webhook-Secret: <SIP_WEBHOOK_SECRET>` header.
```json
{
  "source": "sip",
  "caller_id": "web-client-001",
  "caller_number": "sip:100@asterisk.local"
}
```

**Response (Success):**
```json
{
  "sessionId": "voice-abc123-a1b2c3d4",
  "roomUrl": "https://your-domain.daily.co/voice-abc123",
  "sipUri": "sip:room-id@sip.daily.co",
  "status": "started",
  "message": "Voice session started successfully"
}
```

**Response (Error):**
```json
{
  "error": "Missing required field: callId",
  "status": "error"
}
```

## Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run tests (from infrastructure directory)
pytest src/functions/bot-runner/ -v

# Set environment for local testing
export DAILY_API_KEY_SECRET_ARN=arn:aws:secretsmanager:...
```

## Deployment

The function is deployed automatically as part of the BotRunnerStack:

```bash
cd infrastructure
npm run deploy
```

## Flow Diagram

```
1. Caller dials PSTN number
2. Daily routes call to webhook endpoint
3. Lambda receives POST /start and verifies the Daily signature
4. Lambda creates Daily room (SIP enabled)
5. Lambda generates bot meeting token
6. Lambda calls ECS voice service (POST /call)
7. ECS service spawns pipecat pipeline
8. Pipecat joins Daily room
9. Lambda returns SIP URI to Daily
10. Daily transfers caller to room
11. Voice session begins
```

## Error Handling

| Error | HTTP Code | Cause | Resolution |
|-------|-----------|-------|------------|
| Unauthorized | 401 | Missing, invalid, or stale signature / SIP secret | Check Daily `hmac` matches `DAILY_HMAC_SECRET`; check SIP client sends `X-Sip-Webhook-Secret` |
| Webhook authentication unavailable | 500 | `DAILY_HMAC_SECRET` or `SIP_WEBHOOK_SECRET` unset, or the webhook secret could not be read | Check logs for `Webhook rejected: <reason>` and configure the webhook secret |
| Missing callId | 400 | Webhook payload incomplete | Check Daily configuration |
| Missing callDomain | 400 | Webhook payload incomplete | Check Daily configuration |
| Daily API error | 500 | Room creation failed | Check Daily API key |
| ECS service error | 500 | Service call failed | Check ECS service health and NLB |
| Voice agent unavailable | 503 | Service at capacity | Check auto-scaling and session limits |
