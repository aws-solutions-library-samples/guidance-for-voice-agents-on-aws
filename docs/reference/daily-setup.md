# Daily.co Setup Guide

This guide covers setting up Daily.co for PSTN dial-in with the Voice Agent POC.

## Overview

When someone calls your Daily phone number:

1. **Call arrives** → Daily places caller on hold (music plays)
2. **Webhook triggers** → Daily POSTs to your `room_creation_api` endpoint
3. **Room created** → Your backend creates a Daily room + spawns the bot
4. **Bot joins** → Bot connects to room and emits `dialin-ready`
5. **Call connected** → `pinlessCallUpdate` routes the held call to the room
6. **Conversation starts** → Caller and bot can now talk

## Prerequisites

- Daily.co account at [dashboard.daily.co](https://dashboard.daily.co)
- Credit card on file (required for phone numbers)
- Your API key from Daily dashboard

```bash
export DAILY_API_KEY="your-api-key-here"
```

---

## Step 1: Purchase a Phone Number

### 1.1 List Available Numbers

Find available phone numbers in your desired area:

```bash
# List numbers in California (CA)
curl --request GET \
  --url 'https://api.daily.co/v1/list-available-numbers?region=CA' \
  --header "Authorization: Bearer $DAILY_API_KEY"
```

Response:
```json
{
  "total_count": 50,
  "data": [
    {"number": "+14155551234", "region": "CA"},
    {"number": "+14155555678", "region": "CA"},
    ...
  ]
}
```

### 1.2 Buy a Phone Number

Purchase your chosen number:

```bash
curl --request POST \
  --url 'https://api.daily.co/v1/buy-phone-number' \
  --header "Authorization: Bearer $DAILY_API_KEY" \
  --header 'Content-Type: application/json' \
  --data '{
    "number": "+14155551234"
  }'
```

Response:
```json
{
  "id": "abc123-uuid",
  "number": "+14155551234"
}
```

Save the phone number - you'll need it for the next step.

### 1.3 List Your Phone Numbers

Verify your purchase:

```bash
curl --request GET \
  --url 'https://api.daily.co/v1/phone-numbers' \
  --header "Authorization: Bearer $DAILY_API_KEY"
```

---

## Step 2: Configure Pinless Dial-in

This tells Daily where to send webhook notifications when calls arrive.

### 2.1 Set Up the Webhook

Replace `YOUR_WEBHOOK_URL` with your API Gateway URL:

```bash
curl --request POST \
  --url 'https://api.daily.co/v1' \
  --header "Authorization: Bearer $DAILY_API_KEY" \
  --header 'Content-Type: application/json' \
  --data '{
    "properties": {
      "pinless_dialin": [
        {
          "phone_number": "+14155551234",
          "room_creation_api": "https://YOUR_API_GATEWAY_URL/webhook"
        }
      ]
    }
  }'
```

Response includes an HMAC secret for webhook verification:
```json
{
  "config": {
    "pinless_dialin": [
      {
        "phone_number": "+14155551234",
        "room_creation_api": "https://...",
        "hmac": "base64-encoded-secret"
      }
    ]
  }
}
```

**Save the HMAC secret** as `DAILY_HMAC_SECRET` - the webhook rejects calls it cannot verify with it. See [Webhook Authentication](#webhook-authentication).

### 2.2 Verify Configuration

Check your domain settings:

```bash
curl --request GET \
  --url 'https://api.daily.co/v1' \
  --header "Authorization: Bearer $DAILY_API_KEY"
```

---

## Step 3: Webhook Handler Requirements

When a call comes in, Daily POSTs to your webhook with:

```json
{
  "To": "+14155551234",
  "From": "+15105559876",
  "callId": "call-uuid-here",
  "callDomain": "your-domain.daily.co"
}
```

Your webhook must:

1. **Return 200 quickly** - Daily expects a fast response
2. **Create a Daily room** with SIP enabled
3. **Spawn the bot** to join that room
4. Wait for `dialin-ready` event
5. **Call `pinlessCallUpdate`** to connect the caller

### Webhook Authentication

The `/start` endpoint authenticates every request before it creates a room or starts a voice session. This is implemented in `infrastructure/src/functions/bot-runner/webhook_auth.py`.

| Request | How it is authenticated |
|---------|-------------------------|
| PSTN, from Daily pinless dial-in | Daily HMAC signature (`X-Pinless-Timestamp` + `X-Pinless-Signature`) |
| SIP, body has `"source": "sip"` | Shared secret in the `X-Sip-Webhook-Secret` header |

Unauthenticated requests get `401`. If a required secret is not configured, the request gets `500` and the Lambda logs `Webhook rejected: daily_hmac_secret_not_configured` (or `sip_webhook_secret_not_configured`). The API Gateway stage is also throttled to 10 requests/second (burst 20).

#### Daily signature scheme

From [Daily's pinless dial-in docs](https://docs.daily.co/guides/products/dial-in-dial-out/dialin-pinless#verifying-the-webhook-signature-hmac):

```python
import base64, hashlib, hmac

def daily_signature(hmac_secret: str, timestamp: str, raw_body: bytes) -> str:
    key = base64.b64decode(hmac_secret)  # the secret is base64-encoded
    digest = hmac.new(key, timestamp.encode() + b"." + raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()  # compare with X-Pinless-Signature
```

The handler compares signatures in constant time. It also rejects requests whose `X-Pinless-Timestamp` is more than 5 minutes from the current time, so a captured request cannot be replayed later. Daily's docs do not state the timestamp units, so both epoch seconds and epoch milliseconds are accepted.

Daily also sends a signed `{"test": "test"}` request whenever `pinless_dialin` is configured. The handler acknowledges it with `200` and does not start a session.

#### Configuring the secrets

Both values live in the **webhook secret** in Secrets Manager. Its ARN is in SSM at `/voice-agent/storage/webhook-secret-arn`. Only the BotRunner Lambda can read it, and the Lambda caches it for up to 5 minutes, so allow that long for changes to take effect.

| Key | Value |
|-----|-------|
| `DAILY_HMAC_SECRET` | The `hmac` from your `pinless_dialin` config. Starts as a placeholder; PSTN calls are rejected until you set it. |
| `SIP_WEBHOOK_SECRET` | Random value generated when the Storage stack is deployed. Your SIP client must send it in `X-Sip-Webhook-Secret`. |

**Daily HMAC secret.** `setup-daily.sh` and `update-daily-webhook.sh` save it to `backend/voice-agent/.env` as `DAILY_HMAC_SECRET`. Sync it to AWS with:

```bash
cd infrastructure && ./scripts/init-secrets.sh
```

Daily generates a new `hmac` every time `pinless_dialin` is updated, unless you supply your own base64-encoded value in the `hmac` field. Re-run `init-secrets.sh` after every webhook reconfiguration.

To set it manually instead:

```bash
WEBHOOK_SECRET_ARN=$(aws ssm get-parameter --name /voice-agent/storage/webhook-secret-arn --query 'Parameter.Value' --output text)
NEW_VALUE=$(aws secretsmanager get-secret-value --secret-id "$WEBHOOK_SECRET_ARN" --query SecretString --output text \
  | jq -c --arg h "$DAILY_HMAC_SECRET" '.DAILY_HMAC_SECRET = $h')
aws secretsmanager put-secret-value --secret-id "$WEBHOOK_SECRET_ARN" --secret-string "$NEW_VALUE"
```

**SIP shared secret.** Read the generated value and configure your SIP client (for example the Asterisk dialplan that calls `/start`) to send it:

```bash
WEBHOOK_SECRET_ARN=$(aws ssm get-parameter --name /voice-agent/storage/webhook-secret-arn --query 'Parameter.Value' --output text)
SIP_WEBHOOK_SECRET=$(aws secretsmanager get-secret-value --secret-id "$WEBHOOK_SECRET_ARN" \
  --query SecretString --output text | jq -r .SIP_WEBHOOK_SECRET)

curl -X POST "$WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  -H "X-Sip-Webhook-Secret: $SIP_WEBHOOK_SECRET" \
  -d '{"source": "sip", "caller_id": "web-client-001", "caller_number": "sip:100@asterisk.local"}'
```

To use your own value, add `SIP_WEBHOOK_SECRET=...` to `backend/voice-agent/.env` and run `init-secrets.sh`.

#### Verifying

```bash
cd infrastructure && ./scripts/test-webhook.sh
```

The script checks that an unsigned request is rejected with `401`. It then signs test requests with `DAILY_HMAC_SECRET`, using the env var if set or otherwise the webhook secret.

---

## Step 4: Connect the Call (pinlessCallUpdate)

After the bot joins and `dialin-ready` fires, connect the caller:

```bash
curl --request POST \
  --url 'https://api.daily.co/v1/pinlessCallUpdate' \
  --header "Authorization: Bearer $DAILY_API_KEY" \
  --header 'Content-Type: application/json' \
  --data '{
    "callId": "call-uuid-from-webhook",
    "callDomain": "your-domain.daily.co",
    "sipUri": "sip:room-sip-uri@sip.daily.co"
  }'
```

**Important:** Do NOT call `pinlessCallUpdate` before `dialin-ready` - the call will drop!

---

## Alternative: Global Dial-in Numbers (No Purchase Required)

Daily provides shared phone numbers in 27+ countries. Users dial the number and enter a PIN to join a specific room.

Each room gets a unique 11-digit PIN when created:

```bash
# Create a room
curl --request POST \
  --url 'https://api.daily.co/v1/rooms' \
  --header "Authorization: Bearer $DAILY_API_KEY" \
  --header 'Content-Type: application/json' \
  --data '{
    "name": "my-voice-room",
    "properties": {
      "enable_dialin": true
    }
  }'
```

Response includes dial-in info:
```json
{
  "name": "my-voice-room",
  "url": "https://your-domain.daily.co/my-voice-room",
  "dialin_info": {
    "phone_numbers": ["+1-555-xxx-xxxx", ...],
    "pin": "12345678901"
  }
}
```

---

## Automated Setup Script

The project includes a script that automates the entire setup process:

```bash
# Prerequisites:
# 1. Add DAILY_API_KEY to backend/voice-agent/.env
# 2. Deploy the BotRunner stack (provides webhook URL)

# Run the setup script
cd infrastructure/scripts
./setup-daily.sh

# Or specify a different region
PHONE_REGION=NY ./setup-daily.sh
```

The script will:
1. Load `DAILY_API_KEY` from `backend/voice-agent/.env`
2. Fetch the webhook URL from SSM (deployed infrastructure)
3. List available phone numbers in your region
4. Purchase the first available number (with confirmation)
5. Configure pinless dial-in with your webhook
6. Save `DAILY_PHONE_NUMBER` and `DAILY_HMAC_SECRET` to `.env`

After running, sync secrets to AWS:
```bash
./scripts/init-secrets.sh
```

---

## References

- [Daily REST API Overview](https://docs.daily.co/reference/rest-api)
- [Phone Numbers API](https://docs.daily.co/reference/rest-api/phone-numbers)
- [Pinless Dial-in Guide](https://docs.daily.co/guides/products/dial-in-dial-out/dialin-pinless)
- [Pipecat PSTN Guide](https://docs.pipecat.ai/guides/telephony/daily-pstn)
