#!/bin/bash
# Integration test script for Voice Agent POC
# Run after deployment to validate all components

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
ENVIRONMENT=${ENVIRONMENT:-poc}
AWS_REGION=${AWS_REGION:-us-east-1}
PROJECT_NAME="voice-agent"

echo "======================================"
echo "Voice Agent Integration Tests"
echo "Environment: $ENVIRONMENT"
echo "Region: $AWS_REGION"
echo "======================================"
echo ""

# Track test results
PASSED=0
FAILED=0

# Helper functions
log_pass() {
    echo -e "${GREEN}✓ PASS${NC}: $1"
    ((PASSED++))
}

log_fail() {
    echo -e "${RED}✗ FAIL${NC}: $1"
    ((FAILED++))
}

log_skip() {
    echo -e "${YELLOW}⊘ SKIP${NC}: $1"
}

log_info() {
    echo -e "  → $1"
}

# Test 1: Check SSM Parameters exist
test_ssm_parameters() {
    echo ""
    echo "Test 1: SSM Parameters"
    echo "----------------------"

    local params=(
        "/${PROJECT_NAME}/network/vpc-id"
        "/${PROJECT_NAME}/network/private-subnet-ids"
        "/${PROJECT_NAME}/network/lambda-sg-id"
        "/${PROJECT_NAME}/storage/api-key-secret-arn"
        "/${PROJECT_NAME}/sagemaker/stt-endpoint-name"
        "/${PROJECT_NAME}/sagemaker/tts-endpoint-name"
        "/${PROJECT_NAME}/ecs/service-endpoint"
        "/${PROJECT_NAME}/botrunner/webhook-url"
    )

    for param in "${params[@]}"; do
        if aws ssm get-parameter --name "$param" --region "$AWS_REGION" &>/dev/null; then
            log_pass "SSM parameter exists: $param"
        else
            log_fail "SSM parameter missing: $param"
        fi
    done
}

# Test 2: Check VPC endpoints
test_vpc_endpoints() {
    echo ""
    echo "Test 2: VPC Endpoints"
    echo "---------------------"

    local vpc_id=$(aws ssm get-parameter --name "/${PROJECT_NAME}/network/vpc-id" --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null)

    if [ -z "$vpc_id" ] || [ "$vpc_id" == "None" ]; then
        log_skip "VPC ID not found - skipping endpoint tests"
        return
    fi

    local endpoints=$(aws ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=$vpc_id" --region "$AWS_REGION" --query 'VpcEndpoints[*].ServiceName' --output text 2>/dev/null)

    local expected_services=(
        "sagemaker.runtime"
        "secretsmanager"
        "bedrock-runtime"
        "logs"
        "s3"
    )

    for service in "${expected_services[@]}"; do
        if echo "$endpoints" | grep -q "$service"; then
            log_pass "VPC endpoint exists: $service"
        else
            log_fail "VPC endpoint missing: $service"
        fi
    done
}

# Test 3: Check SageMaker endpoints
test_sagemaker_endpoints() {
    echo ""
    echo "Test 3: SageMaker Endpoints"
    echo "---------------------------"

    local stt_endpoint=$(aws ssm get-parameter --name "/${PROJECT_NAME}/sagemaker/stt-endpoint-name" --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null)
    local tts_endpoint=$(aws ssm get-parameter --name "/${PROJECT_NAME}/sagemaker/tts-endpoint-name" --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null)

    if [ -z "$stt_endpoint" ] || [ "$stt_endpoint" == "None" ]; then
        log_skip "STT endpoint name not found"
    else
        local stt_status=$(aws sagemaker describe-endpoint --endpoint-name "$stt_endpoint" --region "$AWS_REGION" --query 'EndpointStatus' --output text 2>/dev/null)
        if [ "$stt_status" == "InService" ]; then
            log_pass "STT endpoint is InService: $stt_endpoint"
        elif [ -n "$stt_status" ]; then
            log_fail "STT endpoint status: $stt_status (expected InService)"
        else
            log_fail "STT endpoint not found: $stt_endpoint"
        fi
    fi

    if [ -z "$tts_endpoint" ] || [ "$tts_endpoint" == "None" ]; then
        log_skip "TTS endpoint name not found"
    else
        local tts_status=$(aws sagemaker describe-endpoint --endpoint-name "$tts_endpoint" --region "$AWS_REGION" --query 'EndpointStatus' --output text 2>/dev/null)
        if [ "$tts_status" == "InService" ]; then
            log_pass "TTS endpoint is InService: $tts_endpoint"
        elif [ -n "$tts_status" ]; then
            log_fail "TTS endpoint status: $tts_status (expected InService)"
        else
            log_fail "TTS endpoint not found: $tts_endpoint"
        fi
    fi
}

# Test 4: Check API Gateway webhook endpoint
test_webhook_endpoint() {
    echo ""
    echo "Test 4: Webhook Endpoint"
    echo "------------------------"

    local webhook_url=$(aws ssm get-parameter --name "/${PROJECT_NAME}/botrunner/webhook-url" --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null)

    if [ -z "$webhook_url" ] || [ "$webhook_url" == "None" ]; then
        log_skip "Webhook URL not found"
        return
    fi

    log_info "Testing webhook: $webhook_url"

    # Test that endpoint responds (even with error - we just want connectivity).
    # Unsigned requests are rejected with 401.
    local response=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$webhook_url" \
        -H "Content-Type: application/json" \
        -d '{"test": true}' \
        --connect-timeout 10 2>/dev/null)

    if [ "$response" == "401" ] || [ "$response" == "400" ] || [ "$response" == "200" ] || [ "$response" == "500" ]; then
        log_pass "Webhook endpoint is reachable (HTTP $response)"
    else
        log_fail "Webhook endpoint unreachable (HTTP $response)"
    fi
}

# Test 5: Check ECR repository
test_ecr_repository() {
    echo ""
    echo "Test 5: ECR Repository"
    echo "----------------------"

    local repo_name="${PROJECT_NAME}-${ENVIRONMENT}-voice-agent"

    if aws ecr describe-repositories --repository-names "$repo_name" --region "$AWS_REGION" &>/dev/null; then
        log_pass "ECR repository exists: $repo_name"

        # Check if any images exist
        local image_count=$(aws ecr list-images --repository-name "$repo_name" --region "$AWS_REGION" --query 'length(imageIds)' --output text 2>/dev/null)
        if [ "$image_count" -gt 0 ]; then
            log_pass "ECR repository has $image_count image(s)"
        else
            log_info "ECR repository is empty (no images pushed yet)"
        fi
    else
        log_fail "ECR repository not found: $repo_name"
    fi
}

# Test 6: Check Secrets Manager
test_secrets_manager() {
    echo ""
    echo "Test 6: Secrets Manager"
    echo "-----------------------"

    local secret_arn=$(aws ssm get-parameter --name "/${PROJECT_NAME}/storage/api-key-secret-arn" --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null)

    if [ -z "$secret_arn" ] || [ "$secret_arn" == "None" ]; then
        log_skip "Secret ARN not found"
        return
    fi

    if aws secretsmanager describe-secret --secret-id "$secret_arn" --region "$AWS_REGION" &>/dev/null; then
        log_pass "Secret exists: $secret_arn"

        # Check if secret has a value (without revealing it)
        local secret_value=$(aws secretsmanager get-secret-value --secret-id "$secret_arn" --region "$AWS_REGION" --query 'SecretString' --output text 2>/dev/null)
        if [ -n "$secret_value" ] && [ "$secret_value" != "{}" ]; then
            log_pass "Secret has values configured"
        else
            log_info "Secret exists but values may need to be configured"
        fi
    else
        log_fail "Secret not found: $secret_arn"
    fi
}

# Test 7: Check Lambda function
test_lambda_function() {
    echo ""
    echo "Test 7: Lambda Function"
    echo "-----------------------"

    local function_name="${PROJECT_NAME}-${ENVIRONMENT}-bot-runner"

    # Try to find Lambda function by listing and filtering
    local lambda_arn=$(aws lambda list-functions --region "$AWS_REGION" --query "Functions[?contains(FunctionName, 'BotRunner')].FunctionArn" --output text 2>/dev/null | head -1)

    if [ -n "$lambda_arn" ]; then
        log_pass "Lambda function found"
        log_info "ARN: $lambda_arn"

        # Check Lambda configuration
        local runtime=$(aws lambda get-function --function-name "$lambda_arn" --region "$AWS_REGION" --query 'Configuration.Runtime' --output text 2>/dev/null)
        if [ "$runtime" == "python3.11" ]; then
            log_pass "Lambda runtime is Python 3.11"
        else
            log_info "Lambda runtime: $runtime"
        fi
    else
        log_fail "Lambda function not found"
    fi
}

# Test 8: CloudWatch alarms
test_cloudwatch_alarms() {
    echo ""
    echo "Test 8: CloudWatch Alarms"
    echo "-------------------------"

    local alarms=$(aws cloudwatch describe-alarms --region "$AWS_REGION" --query "MetricAlarms[?contains(AlarmName, '${PROJECT_NAME}')].AlarmName" --output text 2>/dev/null)

    if [ -n "$alarms" ]; then
        local alarm_count=$(echo "$alarms" | wc -w)
        log_pass "Found $alarm_count CloudWatch alarm(s)"

        # Check alarm states
        local alarm_states=$(aws cloudwatch describe-alarms --region "$AWS_REGION" --query "MetricAlarms[?contains(AlarmName, '${PROJECT_NAME}')].[AlarmName,StateValue]" --output text 2>/dev/null)
        while IFS=$'\t' read -r name state; do
            if [ "$state" == "OK" ]; then
                log_info "$name: OK"
            elif [ "$state" == "ALARM" ]; then
                log_fail "Alarm in ALARM state: $name"
            else
                log_info "$name: $state"
            fi
        done <<< "$alarm_states"
    else
        log_info "No CloudWatch alarms found (may not be deployed yet)"
    fi
}

# Run all tests
run_all_tests() {
    test_ssm_parameters
    test_vpc_endpoints
    test_sagemaker_endpoints
    test_webhook_endpoint
    test_ecr_repository
    test_secrets_manager
    test_lambda_function
    test_cloudwatch_alarms

    # Summary
    echo ""
    echo "======================================"
    echo "Test Summary"
    echo "======================================"
    echo -e "${GREEN}Passed: $PASSED${NC}"
    echo -e "${RED}Failed: $FAILED${NC}"
    echo ""

    if [ $FAILED -gt 0 ]; then
        echo -e "${RED}Some tests failed. Please review the output above.${NC}"
        exit 1
    else
        echo -e "${GREEN}All tests passed!${NC}"
        exit 0
    fi
}

# Parse command line arguments
case "$1" in
    ssm)
        test_ssm_parameters
        ;;
    vpc)
        test_vpc_endpoints
        ;;
    sagemaker)
        test_sagemaker_endpoints
        ;;
    webhook)
        test_webhook_endpoint
        ;;
    ecr)
        test_ecr_repository
        ;;
    secrets)
        test_secrets_manager
        ;;
    lambda)
        test_lambda_function
        ;;
    alarms)
        test_cloudwatch_alarms
        ;;
    *)
        run_all_tests
        ;;
esac
