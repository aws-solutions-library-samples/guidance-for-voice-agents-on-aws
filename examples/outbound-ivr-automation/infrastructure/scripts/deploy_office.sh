#!/usr/bin/env bash
# Deploy OutboundIvrOfficeStack: the mock provider's own office -- a Connect instance,
# hours of operation, the front-desk queue, its routing and security profiles, the
# outbound-ivr-front-desk agent user and its generated password secret, the office IVR
# contact flow, and (claimOfficeNumber=true, below) the office DID and its flow
# association.
#
# This is a ONE-TIME setup step for most people. A real customer's office is a phone
# system they already run; even here, where this stack stands in for it, the DID it
# claims is meant to be claimed once and kept -- deploying, redeploying, or tearing
# down OutboundIvrStack must never release it, which is exactly why this is its own
# top-level stack instead of living inside OutboundIvrStack. Re-running this script is
# safe: redeploying only updates what actually changed, and CloudFormation does not
# re-claim a new number for a connect.CfnPhoneNumber resource that already exists.
# Claiming a DID at all, though, costs real money -- about $1-2/month while held --
# so do not run this more than once per office number you actually want to keep.
# To release it along with everything else, see scripts/cleanup.sh.
#
#   scripts/deploy_office.sh            deploy OutboundIvrOfficeStack
#   scripts/deploy_office.sh synth      synthesize only, deploy nothing
#   add --yes to skip the confirmation prompt
#
# Deploy this BEFORE scripts/deploy.sh, and independently of it -- there is no
# CloudFormation dependency either way between OutboundIvrOfficeStack and OutboundIvrStack.
# OutboundIvrOfficeStack instead publishes four values to SSM once it has deployed:
#   /outbound-ivr/connect/instance-id            /outbound-ivr/connect/office-number
#   /outbound-ivr/connect/instance-arn           /outbound-ivr/connect/front-desk-queue-arn
# OutboundIvrStack reads all four with the same
# ssm.StringParameter.from_string_parameter_name(...).string_value CloudFormation
# dynamic reference stacks/livekit_stack.py already uses for
# OutboundIvrVoiceStack's own seven /outbound-ivr/voice/* parameters -- resolved by CloudFormation at
# OutboundIvrStack's deploy time, not by this stack reaching into OutboundIvrStack, and not a
# synth-time context-provider lookup either, so neither stack ever needs to exist
# before the other can be synthesized. That is what kills the old two-pass deploy:
# the office number exists (claimed or placeholder) before Outbound IVR is ever synthesized,
# not after.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
export CDK_DOCKER=${CDK_DOCKER:-finch}

synth_only=0
yes=0
for arg in "$@"; do
  case "$arg" in
    synth) synth_only=1 ;;
    --yes|-y) yes=1 ;;
    *) echo "unknown argument: $arg (see the header of $0)" >&2; exit 1 ;;
  esac
done

[ -f .env ] || { echo "missing .env: copy .env.example to .env and fill it in" >&2; exit 1; }
set -a; source .env; set +a
# The account and region this deploys into, from .env (OUTBOUND_IVR_ACCOUNT_ID,
# OUTBOUND_IVR_REGION). Never defaulted: the preflight refuses to run if either is
# unset, or the live credentials disagree with the account.
ACCOUNT=${OUTBOUND_IVR_ACCOUNT_ID:-}
REGION=${OUTBOUND_IVR_REGION:-}
: "${AGENT_EMAIL:?set AGENT_EMAIL in .env (the front desk agent login)}"

# One context list for every cdk command (all three stacks are built each time).
# shellcheck source=app_context.sh
source "$ROOT/infrastructure/scripts/app_context.sh"

if [ "$synth_only" = 1 ]; then
  ensure_dashboard_built
  cdk_app synth OutboundIvrOfficeStack -q
  echo "synth ok: nothing deployed"
  exit 0
fi

# Preflight: right account, right region, CDK bootstrapped, and an explicit
# go-ahead -- same checks scripts/deploy.sh runs before touching AWS.
[ -n "$ACCOUNT" ] || { echo "set OUTBOUND_IVR_ACCOUNT_ID in .env: the account you mean to deploy into" >&2; exit 1; }
[ -n "$REGION" ] || { echo "set OUTBOUND_IVR_REGION in .env: the Region you mean to deploy into" >&2; exit 1; }
caller=$(aws sts get-caller-identity --output json)
caller_account=$(jq -r .Account <<<"$caller")
caller_arn=$(jq -r .Arn <<<"$caller")
if [ "$caller_account" != "$ACCOUNT" ]; then
  echo "credentials are for account $caller_account, expected $ACCOUNT; refresh them and retry" >&2
  exit 1
fi
aws cloudformation describe-stacks --region "$REGION" --stack-name CDKToolkit >/dev/null 2>&1 || {
  echo "account $ACCOUNT is not CDK-bootstrapped in $REGION: run 'cdk bootstrap aws://$ACCOUNT/$REGION' first" >&2
  exit 1; }

echo "deploying OutboundIvrOfficeStack to $ACCOUNT ($REGION) as $caller_arn"
echo "this claims a real phone number for the mock office (about \$1-2/month while"
echo "held) -- a one-time cost for most people. Re-running this script afterward is"
echo "safe and will not claim a second number or release this one."
if [ "$yes" = 0 ]; then
  read -r -p "continue? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "nothing deployed"; exit 1; }
fi

ensure_dashboard_built
cdk_app deploy OutboundIvrOfficeStack --require-approval never

echo
echo "== OutboundIvrOfficeStack deployed. What you need next:"
office_number=$(aws ssm get-parameter --region "$REGION" --name /outbound-ivr/connect/office-number \
  --query 'Parameter.Value' --output text)
instance_id=$(aws ssm get-parameter --region "$REGION" --name /outbound-ivr/connect/instance-id \
  --query 'Parameter.Value' --output text)
echo "  office number:          $office_number"
if [ "$office_number" = "+15555550101" ]; then
  echo "    (that is the unclaimed placeholder, not a real number -- claimOfficeNumber"
  echo "    did not actually claim a DID; check the stack before continuing)"
fi
echo "  Connect instance id:    $instance_id"
echo "  CCP sign-in URL:        https://outbound-ivr-office-${ACCOUNT}.my.connect.aws/ccp-v2/"
echo "  agent password secret:  outbound-ivr/front-desk-agent-password"
echo "    aws secretsmanager get-secret-value --secret-id outbound-ivr/front-desk-agent-password \\"
echo "      --query SecretString --output text --region $REGION"
echo
echo "Sign in to the CCP above as outbound-ivr-front-desk with that password and set yourself"
echo "Available before a call comes in. Now run scripts/deploy.sh: OutboundIvrStack reads"
echo "everything it needs about this office from SSM, not from a stack dependency, so"
echo "it can be deployed, redeployed, or torn down any number of times without ever"
echo "touching what this script just created."
