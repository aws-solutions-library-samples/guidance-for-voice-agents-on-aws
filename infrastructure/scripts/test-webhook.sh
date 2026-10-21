#!/bin/bash
# Test the webhook endpoint with a simulated Daily dial-in payload
# Usage: ./test-webhook.sh [webhook_url]
#
# Requests are signed with DAILY_HMAC_SECRET the same way Daily signs them
# (X-Pinless-Timestamp / X-Pinless-Signature). The secret is read from the
# DAILY_HMAC_SECRET environment variable or, if unset, from the webhook
# secret in Secrets Manager. Signed tests are skipped when no secret is found.

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Configuration
AWS_REGION=${AWS_REGION:-us-east-1}
PROJECT_NAME="voice-agent"

echo -e "${CYAN}======================================"
echo "Voice Agent Webhook Test"
echo -e "======================================${NC}"
echo ""

# Get webhook URL
if [ -n "$1" ]; then
    WEBHOOK_URL="$1"
else
    echo "Fetching webhook URL from SSM..."
    WEBHOOK_URL=$(aws ssm get-parameter --name "/${PROJECT_NAME}/botrunner/webhook-url" --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null)
fi

if [ -z "$WEBHOOK_URL" ] || [ "$WEBHOOK_URL" == "None" ]; then
    echo -e "${RED}Error: Webhook URL not found${NC}"
    echo "Usage: ./test-webhook.sh <webhook_url>"
    echo "  or set /${PROJECT_NAME}/botrunner/webhook-url SSM parameter"
    exit 1
fi

echo -e "Webhook URL: ${CYAN}$WEBHOOK_URL${NC}"

# Get the Daily HMAC secret used to sign test requests
if [ -z "$DAILY_HMAC_SECRET" ]; then
    WEBHOOK_SECRET_ARN=$(aws ssm get-parameter --name "/${PROJECT_NAME}/storage/webhook-secret-arn" --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null || echo "")
    if [ -n "$WEBHOOK_SECRET_ARN" ] && [ "$WEBHOOK_SECRET_ARN" != "None" ]; then
        DAILY_HMAC_SECRET=$(aws secretsmanager get-secret-value --secret-id "$WEBHOOK_SECRET_ARN" --region "$AWS_REGION" --query 'SecretString' --output text 2>/dev/null \
            | jq -r '.DAILY_HMAC_SECRET // empty' 2>/dev/null || echo "")
    fi
fi
if [ "$DAILY_HMAC_SECRET" == "PLACEHOLDER_REPLACE_AFTER_DEPLOY" ]; then
    DAILY_HMAC_SECRET=""
fi

if [ -n "$DAILY_HMAC_SECRET" ]; then
    echo -e "Signing:     ${GREEN}DAILY_HMAC_SECRET loaded${NC}"
else
    echo -e "Signing:     ${YELLOW}DAILY_HMAC_SECRET not configured - signed tests will be skipped${NC}"
fi
echo ""

# Daily signature: base64(HMAC-SHA256(base64_decode(secret), "<timestamp>.<body>"))
sign_payload() {
    local timestamp="$1"
    local body="$2"
    local key_hex
    key_hex=$(printf '%s' "$DAILY_HMAC_SECRET" | base64 -d | od -An -v -tx1 | tr -d ' \n')
    printf '%s.%s' "$timestamp" "$body" \
        | openssl dgst -sha256 -mac HMAC -macopt "hexkey:$key_hex" -binary \
        | base64
}

# POST a payload and set HTTP_CODE / BODY. Pass "signed" as $2 to sign it.
post_payload() {
    local payload="$1"
    local mode="$2"
    local timeout="${3:-10}"
    local auth_headers=()

    if [ "$mode" == "signed" ]; then
        local timestamp
        timestamp=$(date +%s)
        auth_headers=(-H "X-Pinless-Timestamp: $timestamp" -H "X-Pinless-Signature: $(sign_payload "$timestamp" "$payload")")
    fi

    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "$WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        "${auth_headers[@]}" \
        --data-binary "$payload" \
        --connect-timeout "$timeout")

    HTTP_CODE=$(echo "$response" | tail -1)
    BODY=$(echo "$response" | head -n -1)

    echo "Response:"
    echo "$BODY" | jq . 2>/dev/null || echo "$BODY"
    echo ""
    echo -e "HTTP Status: ${HTTP_CODE}"
}

# Generate test data
CALL_ID="test-$(date +%s)"
FROM_NUMBER="+15551234567"
TO_NUMBER="+15559876543"

VALID_PAYLOAD=$(cat <<EOF
{
  "callId": "$CALL_ID",
  "callDomain": "test.daily.co",
  "From": "$FROM_NUMBER",
  "To": "$TO_NUMBER"
}
EOF
)

# Test 1: Unsigned request
echo -e "${YELLOW}Test 1: Unsigned webhook payload (should return 401)${NC}"
echo "-----------------------------------------------------"
echo "Request:"
echo "$VALID_PAYLOAD" | jq .
echo ""
post_payload "$VALID_PAYLOAD" unsigned

if [ "$HTTP_CODE" == "401" ]; then
    echo -e "${GREEN}✓ Test passed - Unsigned request rejected${NC}"
elif [ "$HTTP_CODE" == "500" ]; then
    echo -e "${YELLOW}! Test returned 500 - Check Lambda logs; the webhook secret may not be configured${NC}"
else
    echo -e "${RED}✗ Test failed - Expected 401, got $HTTP_CODE${NC}"
fi
echo ""

if [ -z "$DAILY_HMAC_SECRET" ]; then
    echo -e "${YELLOW}Skipping signed tests - configure DAILY_HMAC_SECRET (see docs/reference/daily-setup.md)${NC}"
else
    # Test 2: Valid signed request
    echo -e "${YELLOW}Test 2: Valid signed webhook payload${NC}"
    echo "------------------------------------"
    post_payload "$VALID_PAYLOAD" signed 30

    if [ "$HTTP_CODE" == "200" ]; then
        echo -e "${GREEN}✓ Test passed - Session started successfully${NC}"
    elif [ "$HTTP_CODE" == "500" ]; then
        echo -e "${YELLOW}! Test returned 500 - Check Lambda logs for details${NC}"
        echo "  This may be expected if Daily/ECS credentials are not configured"
    else
        echo -e "${RED}✗ Test failed - Unexpected HTTP status${NC}"
    fi
    echo ""

    # Test 3: Missing callId
    echo -e "${YELLOW}Test 3: Signed, missing callId (should return 400)${NC}"
    echo "--------------------------------------------------"
    post_payload '{"callDomain": "test.daily.co", "From": "+15551234567"}' signed

    if [ "$HTTP_CODE" == "400" ]; then
        echo -e "${GREEN}✓ Test passed - Correctly returned 400 for missing callId${NC}"
    else
        echo -e "${RED}✗ Test failed - Expected 400, got $HTTP_CODE${NC}"
    fi
    echo ""

    # Test 4: Missing callDomain
    echo -e "${YELLOW}Test 4: Signed, missing callDomain (should return 400)${NC}"
    echo "------------------------------------------------------"
    post_payload '{"callId": "test-123", "From": "+15551234567"}' signed

    if [ "$HTTP_CODE" == "400" ]; then
        echo -e "${GREEN}✓ Test passed - Correctly returned 400 for missing callDomain${NC}"
    else
        echo -e "${RED}✗ Test failed - Expected 400, got $HTTP_CODE${NC}"
    fi
    echo ""
fi

# Test 5: Invalid JSON
echo -e "${YELLOW}Test 5: Invalid JSON (should return 400)${NC}"
echo "-----------------------------------------"
post_payload 'not valid json {' unsigned

if [ "$HTTP_CODE" == "400" ]; then
    echo -e "${GREEN}✓ Test passed - Correctly returned 400 for invalid JSON${NC}"
else
    echo -e "${RED}✗ Test failed - Expected 400, got $HTTP_CODE${NC}"
fi

echo ""
echo -e "${CYAN}======================================"
echo "Tests Complete"
echo -e "======================================${NC}"
