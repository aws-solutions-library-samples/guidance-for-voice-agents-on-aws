#!/bin/bash
# Initialize secrets for Voice Agent POC
# This script helps configure API keys in Secrets Manager
#
# Supports:
#   - DEEPGRAM_API_KEY: For STT (Speech-to-Text)
#   - CARTESIA_API_KEY: For TTS (Text-to-Speech)
#   - DAILY_API_KEY: For Daily.co WebRTC transport
#   - DAILY_HMAC_SECRET: Daily pinless dial-in HMAC, verifies PSTN webhooks
#   - SIP_WEBHOOK_SECRET: Shared secret for SIP requests (optional override;
#     a random value is generated when the Storage stack is deployed)

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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../../backend/voice-agent/.env"

echo -e "${CYAN}======================================"
echo "Voice Agent Secrets Initialization"
echo -e "======================================${NC}"
echo ""

# Get secret ARN from SSM
echo "Fetching secret ARN from SSM..."
SECRET_ARN=$(aws ssm get-parameter \
    --name "/${PROJECT_NAME}/storage/api-key-secret-arn" \
    --region "$AWS_REGION" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null)

if [ -z "$SECRET_ARN" ] || [ "$SECRET_ARN" == "None" ]; then
    echo -e "${RED}Error: Secret ARN not found in SSM${NC}"
    echo "Make sure the Storage stack is deployed first:"
    echo "  ./deploy.sh deploy-stack VoiceAgentStorage"
    exit 1
fi

echo -e "Secret ARN: ${CYAN}$SECRET_ARN${NC}"
echo ""

# Try to load from .env file if it exists
if [ -f "$ENV_FILE" ]; then
    echo -e "${GREEN}Found .env file at: $ENV_FILE${NC}"
    echo "Loading API keys from .env..."

    # Source the .env file to get variables
    set -a
    source "$ENV_FILE"
    set +a
    echo ""
fi

# Check if running interactively and keys not set
if [ -t 0 ]; then
    # Interactive mode - prompt for missing values
    echo -e "${YELLOW}Enter your API keys (press Enter to keep existing value):${NC}"
    echo ""

    # Deepgram API Key (STT)
    if [ -n "$DEEPGRAM_API_KEY" ]; then
        echo -e "Deepgram API Key: ${GREEN}[loaded from .env]${NC}"
    else
        echo -n "Deepgram API Key: "
        read -s DEEPGRAM_API_KEY
        echo ""
    fi

    # Cartesia API Key (TTS)
    if [ -n "$CARTESIA_API_KEY" ]; then
        echo -e "Cartesia API Key: ${GREEN}[loaded from .env]${NC}"
    else
        echo -n "Cartesia API Key: "
        read -s CARTESIA_API_KEY
        echo ""
    fi

    # Daily API Key (optional)
    if [ -n "$DAILY_API_KEY" ]; then
        echo -e "Daily API Key: ${GREEN}[loaded from .env]${NC}"
    else
        echo -n "Daily API Key (optional, press Enter to skip): "
        read -s DAILY_API_KEY
        echo ""
    fi
    echo ""
fi

# Validate required keys
if [ -z "$DEEPGRAM_API_KEY" ] && [ -z "$CARTESIA_API_KEY" ]; then
    echo -e "${RED}Error: At least DEEPGRAM_API_KEY or CARTESIA_API_KEY must be set${NC}"
    echo "Set them in backend/voice-agent/.env or as environment variables"
    exit 1
fi

# Build secret value JSON
SECRET_VALUE=$(cat <<EOF
{
  "DEEPGRAM_API_KEY": "${DEEPGRAM_API_KEY:-}",
  "CARTESIA_API_KEY": "${CARTESIA_API_KEY:-}",
  "DAILY_API_KEY": "${DAILY_API_KEY:-}"
}
EOF
)

# Update secret
echo "Updating secret in Secrets Manager..."

if aws secretsmanager put-secret-value \
    --secret-id "$SECRET_ARN" \
    --secret-string "$SECRET_VALUE" \
    --region "$AWS_REGION" > /dev/null 2>&1; then

    echo -e "${GREEN}✓ Secrets updated successfully${NC}"
    echo ""
    echo "Configured secrets:"
    echo "  - DEEPGRAM_API_KEY (STT): $([ -n "$DEEPGRAM_API_KEY" ] && echo "✓ Set" || echo "- Not set")"
    echo "  - CARTESIA_API_KEY (TTS): $([ -n "$CARTESIA_API_KEY" ] && echo "✓ Set" || echo "- Not set")"
    echo "  - DAILY_API_KEY (WebRTC): $([ -n "$DAILY_API_KEY" ] && echo "✓ Set" || echo "- Not set (optional)")"
else
    echo -e "${RED}✗ Failed to update secrets${NC}"
    echo "Check your AWS credentials and permissions"
    exit 1
fi

# Update webhook authentication secret (read only by the BotRunner Lambda).
# Only keys set in .env are written; other keys keep their current value.
if [ "$DAILY_HMAC_SECRET" == "null" ]; then
    DAILY_HMAC_SECRET=""
fi
echo ""
echo "Updating webhook authentication secret..."
WEBHOOK_SECRET_ARN=$(aws ssm get-parameter \
    --name "/${PROJECT_NAME}/storage/webhook-secret-arn" \
    --region "$AWS_REGION" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || echo "")

if [ -z "$WEBHOOK_SECRET_ARN" ] || [ "$WEBHOOK_SECRET_ARN" == "None" ]; then
    echo -e "${YELLOW}! Webhook secret not found in SSM - redeploy the Storage stack:${NC}"
    echo "    ./deploy.sh deploy-stack VoiceAgentStorage"
elif [ -z "$DAILY_HMAC_SECRET" ] && [ -z "$SIP_WEBHOOK_SECRET" ]; then
    echo -e "${YELLOW}! DAILY_HMAC_SECRET not set - PSTN calls are rejected until it is configured${NC}"
    echo "  Run ./scripts/setup-daily.sh (or add DAILY_HMAC_SECRET to .env) and re-run this script"
elif ! command -v jq &> /dev/null; then
    echo -e "${RED}✗ jq is required to update the webhook secret${NC}"
    echo "Install with: brew install jq (macOS) or apt-get install jq (Linux)"
    exit 1
else
    CURRENT_WEBHOOK_SECRET=$(aws secretsmanager get-secret-value \
        --secret-id "$WEBHOOK_SECRET_ARN" \
        --region "$AWS_REGION" \
        --query 'SecretString' \
        --output text)
    WEBHOOK_SECRET_VALUE=$(echo "$CURRENT_WEBHOOK_SECRET" | jq -c \
        --arg hmac "${DAILY_HMAC_SECRET:-}" \
        --arg sip "${SIP_WEBHOOK_SECRET:-}" \
        '(if $hmac != "" then .DAILY_HMAC_SECRET = $hmac else . end)
         | (if $sip != "" then .SIP_WEBHOOK_SECRET = $sip else . end)')

    if aws secretsmanager put-secret-value \
        --secret-id "$WEBHOOK_SECRET_ARN" \
        --secret-string "$WEBHOOK_SECRET_VALUE" \
        --region "$AWS_REGION" > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Webhook secret updated successfully${NC}"
        echo "  - DAILY_HMAC_SECRET (PSTN): $([ -n "$DAILY_HMAC_SECRET" ] && echo "✓ Set" || echo "- Unchanged")"
        echo "  - SIP_WEBHOOK_SECRET (SIP): $([ -n "$SIP_WEBHOOK_SECRET" ] && echo "✓ Set" || echo "- Unchanged")"
        echo "  The webhook Lambda picks up new values within 5 minutes."
    else
        echo -e "${RED}✗ Failed to update webhook secret${NC}"
        echo "Check your AWS credentials and permissions"
        exit 1
    fi
fi

echo ""
echo -e "${GREEN}======================================"
echo "Secrets Configuration Complete"
echo -e "======================================${NC}"
echo ""
echo "The ECS service will load these secrets automatically on startup."
echo ""
echo "Webhook endpoint:"
WEBHOOK_URL=$(aws ssm get-parameter \
    --name "/${PROJECT_NAME}/botrunner/webhook-url" \
    --region "$AWS_REGION" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || echo "<deploy BotRunner stack first>")
echo "   $WEBHOOK_URL"
echo ""
echo "To verify deployment:"
echo "   ./scripts/test-webhook.sh"
