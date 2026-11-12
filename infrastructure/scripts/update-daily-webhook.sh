#!/bin/bash
# update-daily-webhook.sh - Update Daily.co webhook URL
#
# This script updates the webhook URL for an existing Daily.co phone number
# Use this when the Bot Runner endpoint changes (e.g., after redeployment)
#
# Prerequisites:
#   - DAILY_API_KEY in backend/voice-agent/.env
#   - DAILY_PHONE_NUMBER in backend/voice-agent/.env
#   - BotRunner stack deployed
#
# Usage:
#   ./update-daily-webhook.sh

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
AWS_PROFILE=${AWS_PROFILE:-default}

# Export AWS profile for CLI commands
export AWS_PROFILE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../../backend/voice-agent/.env"

echo -e "${CYAN}======================================"
echo "Daily.co Webhook Update"
echo -e "======================================${NC}"
echo ""

# Check for jq
if ! command -v jq &> /dev/null; then
    echo -e "${RED}Error: jq is required but not installed.${NC}"
    echo "Install with: brew install jq (macOS) or apt-get install jq (Linux)"
    exit 1
fi

# Load configuration from .env
if [ -f "$ENV_FILE" ]; then
    echo -e "Loading configuration from ${CYAN}$ENV_FILE${NC}"
    set -a
    source "$ENV_FILE"
    set +a
else
    echo -e "${RED}Error: .env file not found at $ENV_FILE${NC}"
    exit 1
fi

# Validate DAILY_API_KEY
if [ -z "$DAILY_API_KEY" ]; then
    echo -e "${RED}Error: DAILY_API_KEY not found in .env${NC}"
    echo "Add DAILY_API_KEY=your-key to $ENV_FILE"
    exit 1
fi

# Validate DAILY_PHONE_NUMBER
if [ -z "$DAILY_PHONE_NUMBER" ]; then
    echo -e "${RED}Error: DAILY_PHONE_NUMBER not found in .env${NC}"
    echo "Run setup-daily.sh first to purchase a phone number"
    exit 1
fi

echo -e "DAILY_API_KEY: ${GREEN}[loaded from .env]${NC}"
echo -e "Phone Number: ${GREEN}$DAILY_PHONE_NUMBER${NC}"
echo ""

# Fetch webhook URL from CloudFormation
echo "Fetching webhook URL from CloudFormation..."
WEBHOOK_URL=$(aws cloudformation describe-stacks \
    --stack-name VoiceAgentBotRunner \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='WebhookEndpoint'].OutputValue" \
    --output text 2>&1)

# Check if result is empty or contains error
if [ -z "$WEBHOOK_URL" ] || [[ "$WEBHOOK_URL" == *"error"* ]] || [[ "$WEBHOOK_URL" == *"Error"* ]] || [[ "$WEBHOOK_URL" == *"does not exist"* ]]; then
    WEBHOOK_URL=""
fi

if [ -z "$WEBHOOK_URL" ] || [ "$WEBHOOK_URL" == "None" ]; then
    echo -e "${RED}Error: Webhook URL not found in CloudFormation${NC}"
    echo "Make sure the BotRunner stack is deployed:"
    echo "  cd infrastructure && npx cdk deploy VoiceAgentBotRunner"
    exit 1
fi
echo -e "New Webhook URL: ${GREEN}$WEBHOOK_URL${NC}"
echo ""

# Update webhook configuration
echo -e "${CYAN}Updating Daily.co webhook configuration...${NC}"
CONFIG=$(curl -s --request POST \
  --url 'https://api.daily.co/v1' \
  --header "Authorization: Bearer $DAILY_API_KEY" \
  --header 'Content-Type: application/json' \
  --data "{
    \"properties\": {
      \"pinless_dialin\": [{
        \"phone_number\": \"$DAILY_PHONE_NUMBER\",
        \"room_creation_api\": \"$WEBHOOK_URL\"
      }]
    }
  }")

# Check for errors
if echo "$CONFIG" | jq -e '.error' > /dev/null 2>&1; then
    echo -e "${RED}Error configuring webhook: $(echo $CONFIG | jq -r '.error')${NC}"
    exit 1
fi

HMAC=$(echo "$CONFIG" | jq -r '(.config // .properties).pinless_dialin[0].hmac')
echo -e "${GREEN}Webhook updated successfully!${NC}"
echo ""

# Update HMAC in .env if it changed
if [ -n "$HMAC" ] && [ "$HMAC" != "null" ]; then
    echo -e "${CYAN}Updating HMAC secret in .env...${NC}"
    
    # Remove old HMAC value
    sed -i.bak '/^DAILY_HMAC_SECRET=/d' "$ENV_FILE" 2>/dev/null || true
    rm -f "${ENV_FILE}.bak"
    
    # Add new HMAC value
    echo "" >> "$ENV_FILE"
    echo "# Updated by update-daily-webhook.sh on $(date)" >> "$ENV_FILE"
    echo "DAILY_HMAC_SECRET=$HMAC" >> "$ENV_FILE"
    
    echo -e "${GREEN}HMAC secret updated${NC}"
fi

echo ""
echo -e "${GREEN}======================================"
echo "Update Complete"
echo -e "======================================${NC}"
echo ""
echo "Configuration:"
echo -e "  Phone Number:  ${CYAN}$DAILY_PHONE_NUMBER${NC}"
echo -e "  Webhook URL:   ${CYAN}$WEBHOOK_URL${NC}"
echo ""
echo "Next steps:"
echo "  1. Run ./scripts/init-secrets.sh to sync secrets to AWS"
echo "  2. Test by calling ${CYAN}$DAILY_PHONE_NUMBER${NC}"
echo ""
