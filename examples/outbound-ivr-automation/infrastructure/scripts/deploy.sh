#!/usr/bin/env bash
# Deploy Outbound IVR Automation, in order:
#   1. OutboundIvrVoiceStack   the Chime SDK Voice Connector + EIP pool, meant to be
#                              deployed once and kept while OutboundIvrStack is
#                              redeployed on its own. See scripts/cleanup.sh to
#                              tear everything, including this stack, down.
#   2. OutboundIvrStack        one top-level stack -- network, LiveKit on ECS,
#      escalation (the evidence team) on the Connect instance OutboundIvrOfficeStack
#      owns, the agent Runtime, data, the tools Gateway, and the hosted dashboard,
#      as nested children (see stacks/outbound_ivr_stack.py). The LiveKit host's
#      private IP is fixed at synth time (a launch template network-interface
#      override, not an SSM parameter written after the task starts), so the
#      agent Runtime's LIVEKIT_URL is known before this app's first deploy.
#
#   Before either step, ONCE and on its own: deploy_office.sh, which deploys
#   OutboundIvrOfficeStack -- the mock provider's own Connect instance, front desk,
#   and (normally) its claimed office DID. It is a separate top-level stack with no
#   CloudFormation dependency either way between it and OutboundIvrStack (see its
#   own header), so it is not one of the steps below; step 2 here just fails fast,
#   pointing back at it, if it has never been deployed.
#
#   scripts/deploy.sh                 both steps
#   scripts/deploy.sh voice           step 1 only
#   scripts/deploy.sh outbound_ivr    step 2 only
#   scripts/deploy.sh synth           synthesize both, deploy nothing
#   add --yes to skip the confirmation prompt
#
# OutboundIvrVoiceStack deploys with --no-rollback: a failed first create keeps
# what it made, and the next run continues instead of recreating (and orphaning)
# retained resources. OutboundIvrStack keeps rollback on: its LiveKit nested
# stack's ECS updates can replace task definitions, which --no-rollback refuses.
# Phone numbers and the email come from .env, never this file.
#
# Container builds use finch, not docker: set CDK_DOCKER=finch (the default
# below) before this script runs `cdk deploy`/`cdk synth`, which CDK's own Docker
# bundling honors for the agent Runtime's image asset.
#
# Monitor (the private "listen in" transport -- S3 segments polled through the
# dashboard API, not a CloudFront distribution or a WebSocket) always deploys as
# part of OutboundIvrStack; it has no .env gate of its own. Listen-in audio lags
# the live call by about 3-6 seconds. The Connect instance itself is never built
# here at all -- deploy_office.sh's OutboundIvrOfficeStack owns it, as its own
# top-level stack. FrontDesk still builds as a nested child, but only for the
# dashboard's CCP approved-origin grant now; the queue, agent, flow, and claimed
# DID it used to carry all moved to OutboundIvrOfficeStack too (see
# frontdesk_stack.py's own module docstring). This script claims the evidence
# team's DID by default (claimEvidenceNumber below), the only way it is actually
# reachable by phone; about $1-2/month while claimed. The mock office's own DID is
# claimed once by deploy_office.sh, not here.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
export CDK_DOCKER=${CDK_DOCKER:-finch}

step=all
yes=0
for arg in "$@"; do
  case "$arg" in
    all|voice|outbound_ivr|synth) step=$arg ;;
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
: "${FRONT_DESK_NUMBER:?set FRONT_DESK_NUMBER in .env}"
: "${AGENT_EMAIL:?set AGENT_EMAIL in .env (the escalation agent login)}"
: "${CALLER_ID_NUMBER:?set CALLER_ID_NUMBER in .env: the Chime SDK number to call from (see the README Prerequisites)}"
[[ "$FRONT_DESK_NUMBER" =~ ^\+[1-9][0-9]{7,14}$ ]] || {
  echo "phone numbers must be E.164, like +15555550100" >&2; exit 1; }

# One context list for every cdk command (all three stacks are built each time).
# shellcheck source=app_context.sh
source "$ROOT/infrastructure/scripts/app_context.sh"

if [ "$step" = synth ]; then
  build_dashboard
  cdk_app synth --all -q
  echo "synth ok: nothing deployed"
  exit 0
fi

# Preflight: right account, right region, CDK bootstrapped, and an explicit go-ahead.
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

# OutboundIvrStack has no CloudFormation dependency on OutboundIvrOfficeStack -- it
# reads what it needs about the office (the Connect instance, the claimed office
# number, the front-desk queue arn) straight from SSM at deploy time, which only
# resolves if deploy_office.sh has actually written those parameters at least
# once. Checked here, for the steps that actually touch OutboundIvrStack, rather
# than left to surface as an opaque CloudFormation dynamic-reference-parameter
# failure mid-deploy -- or, worse, only after step 'all' has already spent time
# deploying OutboundIvrVoiceStack first.
if [ "$step" = outbound_ivr ] || [ "$step" = all ]; then
  aws ssm get-parameter --region "$REGION" --name /outbound-ivr/connect/instance-id >/dev/null 2>&1 || {
    echo "OutboundIvrOfficeStack has not been deployed yet (no /outbound-ivr/connect/instance-id" >&2
    echo "in SSM in $REGION): run scripts/deploy_office.sh first, then retry." >&2
    exit 1; }
fi

echo "deploying step '$step' to $ACCOUNT ($REGION) as $caller_arn"
if [ "$yes" = 0 ]; then
  read -r -p "continue? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "nothing deployed"; exit 1; }
fi

deploy_voice() {
  echo "== 1/2 OutboundIvrVoiceStack"
  ensure_dashboard_built
  cdk_app deploy OutboundIvrVoiceStack --require-approval never --no-rollback
}

deploy_outbound_ivr() {
  echo "== 2/2 OutboundIvrStack"
  build_dashboard
  # NO_ROLLBACK=1 for a first create: keep what succeeded and resume after a fix.
  local rollback=()
  [ "${NO_ROLLBACK:-0}" = 1 ] && rollback=(--no-rollback)
  cdk_app deploy OutboundIvrStack --require-approval never ${rollback[@]+"${rollback[@]}"}
  # No flow push happens here at all -- the office IVR flow is
  # OutboundIvrOfficeStack's resource now, not OutboundIvrStack's, so it is
  # deploy_office.sh's `cdk deploy` that pushes current infrastructure/connect/flows/
  # content via CloudFormation, on its own, independent schedule -- see that
  # script's own header. deploy_flow.sh still exists for a faster,
  # CloudFormation-bypassing push straight to Connect when you only changed
  # infrastructure/connect/flows/ and don't want to redeploy
  # OutboundIvrOfficeStack for it; see its own header for why that still works
  # unchanged against the SSM contract above.
}

case "$step" in
  all) deploy_voice; deploy_outbound_ivr ;;
  voice) deploy_voice ;;
  outbound_ivr) deploy_outbound_ivr ;;
esac
echo "done"
