#!/usr/bin/env bash
# Tear down everything this example creates: OutboundIvrStack, OutboundIvrOfficeStack,
# and OutboundIvrVoiceStack, plus the data these stacks' buckets hold that `cdk destroy`
# alone cannot remove (two of the three S3 buckets are DESTROY with no auto-delete, on
# purpose -- see data_construct.py and monitor_stack.py -- so a non-empty bucket simply
# fails to delete instead of silently losing call recordings or Monitor segments nobody
# got a chance to look at first).
#
# Destroys in reverse dependency order: OutboundIvrStack first (it is the one stack
# redeployed/torn down routinely), then OutboundIvrOfficeStack (the mock office --
# normally long-lived, see deploy_office.sh's own header), then OutboundIvrVoiceStack
# (the Chime SDK Voice Connector + EIP pool -- normally deployed once and kept, see
# deploy.sh's own header). This script is for tearing down ALL of it, including the two
# stacks the normal deploy scripts are careful never to touch.
#
#   scripts/cleanup.sh            print what would be deleted/destroyed; delete nothing
#   scripts/cleanup.sh --yes      actually do it
#
# Reads AWS_PROFILE and the account/region to operate in from the environment -- the
# same .env this example's deploy scripts read (OUTBOUND_IVR_ACCOUNT_ID,
# OUTBOUND_IVR_REGION), plus FRONT_DESK_NUMBER/AGENT_EMAIL/EVIDENCE_MODE/CALLER_ID_NUMBER
# so `cdk destroy`'s own synth pass builds the exact same stacks (same context, same
# logical ids) that were actually deployed -- a context mismatch here is a synth-time
# resource-topology mismatch (claimEvidenceNumber/claimOfficeNumber are both
# unconditionally true below, matching deploy.sh/deploy_office.sh's own defaults; edit
# both scripts' context-building the same way if you ever deployed with either off).
#
# Always prints, at the end, exactly what this script cannot remove and the command to
# remove each -- see "WHAT THIS SCRIPT CANNOT REMOVE" below for the reasons.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
export CDK_DOCKER=${CDK_DOCKER:-finch}

yes=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) yes=1 ;;
    *) echo "unknown argument: $arg (see the header of $0)" >&2; exit 1 ;;
  esac
done

[ -f .env ] || { echo "missing .env: copy .env.example to .env and fill it in" >&2; exit 1; }
set -a; source .env; set +a
ACCOUNT=${OUTBOUND_IVR_ACCOUNT_ID:-}
REGION=${OUTBOUND_IVR_REGION:-}
[ -n "$ACCOUNT" ] || { echo "set OUTBOUND_IVR_ACCOUNT_ID in .env: the account to clean up" >&2; exit 1; }
[ -n "$REGION" ] || { echo "set OUTBOUND_IVR_REGION in .env: the Region to clean up" >&2; exit 1; }
: "${FRONT_DESK_NUMBER:?set FRONT_DESK_NUMBER in .env}"
: "${AGENT_EMAIL:?set AGENT_EMAIL in .env}"

caller=$(aws sts get-caller-identity --output json)
caller_account=$(jq -r .Account <<<"$caller")
caller_arn=$(jq -r .Arn <<<"$caller")
if [ "$caller_account" != "$ACCOUNT" ]; then
  echo "credentials are for account $caller_account, expected $ACCOUNT; refresh them and retry" >&2
  exit 1
fi

# The same context deploy.sh and deploy_office.sh use, so `cdk destroy`'s own synth
# pass matches what is actually deployed.
# shellcheck source=app_context.sh
source "$ROOT/infrastructure/scripts/app_context.sh"

# The two buckets that are DESTROY with no auto-delete, by design (see
# data_construct.py's ArtifactsBucket and monitor_stack.py's MonitorBucket) -- `cdk
# destroy` simply fails on a non-empty one of these instead of silently erasing
# anything still live. The dashboard's own SiteBucket has auto_delete_objects=True
# already and needs nothing here.
artifacts_bucket="outbound-ivr-artifacts-${ACCOUNT}"
monitor_bucket="outbound-ivr-monitor-${ACCOUNT}"

empty_bucket() {
  local bucket="$1"
  aws s3api head-bucket --bucket "$bucket" --region "$REGION" 2>/dev/null || {
    echo "  $bucket: does not exist, nothing to empty"
    return 0
  }
  echo "  emptying $bucket (every object version and delete marker)"
  # --no-paginate: the AWS CLI's own generic pagination (--max-items/--starting-token)
  # uses an opaque continuation token for this API, not the key-marker/version-id-marker
  # pair list-object-versions actually takes -- walking pages by hand below needs the
  # real ones, so auto-pagination has to stay off.
  local next_key_marker="" next_version_id_marker="" extra_args=()
  while :; do
    extra_args=()
    [ -n "$next_key_marker" ] && extra_args+=(--key-marker "$next_key_marker")
    [ -n "$next_version_id_marker" ] && extra_args+=(--version-id-marker "$next_version_id_marker")
    local page
    page=$(aws s3api list-object-versions --bucket "$bucket" --region "$REGION" --no-paginate \
      "${extra_args[@]+${extra_args[@]}}" --output json)
    # delete-objects caps a single call at 1000 keys; batch this page's versions and
    # delete markers into chunks that size.
    local to_delete_all count
    to_delete_all=$(jq -c '(.Versions // []) + (.DeleteMarkers // []) | map({Key, VersionId})' <<<"$page")
    count=$(jq 'length' <<<"$to_delete_all")
    local offset=0
    while [ "$offset" -lt "$count" ]; do
      local chunk
      chunk=$(jq -c --argjson offset "$offset" '{Objects: .[$offset:$offset+1000], Quiet: true}' <<<"$to_delete_all")
      aws s3api delete-objects --bucket "$bucket" --region "$REGION" --delete "$chunk" >/dev/null
      offset=$((offset + 1000))
    done
    [ "$(jq -r '.IsTruncated' <<<"$page")" = "true" ] || break
    next_key_marker=$(jq -r '.NextKeyMarker // ""' <<<"$page")
    next_version_id_marker=$(jq -r '.NextVersionIdMarker // ""' <<<"$page")
  done
}

destroy_outbound_ivr() { ensure_dashboard_built; cdk_app destroy OutboundIvrStack --force; }
destroy_office() { ensure_dashboard_built; cdk_app destroy OutboundIvrOfficeStack --force; }
destroy_voice() { ensure_dashboard_built; cdk_app destroy OutboundIvrVoiceStack --force; }

print_survivors() {
  cat <<EOF

== WHAT THIS SCRIPT CANNOT REMOVE ==

These are never CloudFormation resources, so no stack deletion above touches them.
Confirm each still exists before running its command -- some only appear if you
actually exercised that path.

1. The implicit CloudWatch Logs log groups Lambda creates on a function's first
   invocation, for every Lambda this app builds without an explicit \`logs.LogGroup\`:
   voice_stack.py's ChimeTerminationCredentialsHandler, the shared AwsCustomResource
   singleton Lambda voice_stack.py/office_stack.py/escalation_stack.py/
   frontdesk_stack.py all use for Chime/Connect AWS SDK calls, and the custom_resources
   Provider framework's own "framework-onEvent" Lambda. Find and delete them with:
     aws logs describe-log-groups --region $REGION \\
       --log-group-name-prefix /aws/lambda/ \\
       --query "logGroups[?contains(logGroupName, 'OutboundIvr') || contains(logGroupName, 'AWS679f53fac002430cb0da5b7982bd2287')].logGroupName" \\
       --output text | tr '\\t' '\\n' | while read -r lg; do
         aws logs delete-log-group --region $REGION --log-group-name "\$lg"
       done

2. The Amazon Bedrock AgentCore Runtime's and Gateway's own managed CloudWatch Logs
   log groups (under /aws/bedrock-agentcore/...), created by the AgentCore service
   itself on first invocation, not by this app's CloudFormation. Find and delete them
   with:
     aws logs describe-log-groups --region $REGION \\
       --log-group-name-prefix /aws/bedrock-agentcore/ \\
       --query 'logGroups[].logGroupName' --output text | tr '\\t' '\\n' | while read -r lg; do
         aws logs delete-log-group --region $REGION --log-group-name "\$lg"
       done

3. The Chime SDK caller-ID number in CALLER_ID_NUMBER. Destroying OutboundIvrVoiceStack
   only releases it from the Voice Connector; it stays in your account (and on your
   bill) until you delete it:
     aws chime-sdk-voice delete-phone-number --region $REGION --phone-number-id "$CALLER_ID_NUMBER"

4. Claimed phone numbers (the mock office DID and, if claimed, the evidence team's
   DID): each is a real \`connect.CfnPhoneNumber\` resource, so OutboundIvrOfficeStack's
   and OutboundIvrStack's own deletion above already releases them in the normal case.
   If either destroy above failed partway through, or a number still shows CLAIMED
   afterward, release it by hand:
     aws connect list-phone-numbers-v2 --region $REGION \\
       --query 'ListPhoneNumbersSummaryList[].[PhoneNumber,PhoneNumberId]' --output table
     aws connect release-phone-number --region $REGION --phone-number-id <PhoneNumberId>

5. CDK bootstrap assets: every \`cdk deploy\` of this app (container images, Lambda
   bundles) uploads to the bootstrap staging bucket \`cdk-hnb659fds-assets-${ACCOUNT}-${REGION}\`,
   which none of the destroys above touch -- it is shared by every app in this
   account/Region, not owned by this example. Safe to leave; to reclaim the storage
   this example's own assets used:
     aws s3 rm "s3://cdk-hnb659fds-assets-${ACCOUNT}-${REGION}/" --recursive
   Only run that if nothing else in this account/Region still depends on the CDK
   bootstrap (it empties the bucket for every app, not just this one). The bootstrap
   stack itself (\`CDKToolkit\`) is infrastructure this example did not create; leave it
   unless you are decommissioning CDK from this account/Region entirely.
EOF
}

if [ "$yes" = 0 ]; then
  echo "DRY RUN (pass --yes to actually do this) -- would, in order:"
  echo "  1. empty s3://$artifacts_bucket and s3://$monitor_bucket"
  echo "  2. cdk destroy OutboundIvrStack"
  echo "  3. cdk destroy OutboundIvrOfficeStack"
  echo "  4. cdk destroy OutboundIvrVoiceStack"
  echo "as $caller_arn, account $ACCOUNT, region $REGION"
  print_survivors
  exit 0
fi

echo "cleaning up $ACCOUNT ($REGION) as $caller_arn"
echo "== 1/4 emptying buckets"
empty_bucket "$artifacts_bucket"
empty_bucket "$monitor_bucket"
echo "== 2/4 destroying OutboundIvrStack"
if ! destroy_outbound_ivr; then
  # The usual cause: the AgentCore Runtime's VPC network interfaces (interface type
  # agentic_ai) still hold its security group. AgentCore removes them up to 8 hours
  # after the Runtime is deleted, and nobody can delete them sooner.
  vpcs=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=tag:project,Values=outbound-ivr \
    --query 'Vpcs[].VpcId' --output text)
  agentic_enis=0
  for vpc in $vpcs; do
    n=$(aws ec2 describe-network-interfaces --region "$REGION" --filters "Name=vpc-id,Values=$vpc" \
      --query "length(NetworkInterfaces[?InterfaceType=='agentic_ai'])" --output text)
    agentic_enis=$((agentic_enis + n))
  done
  if [ "$agentic_enis" -gt 0 ]; then
    cat >&2 <<EOF

OutboundIvrStack could not finish deleting yet. $agentic_enis AgentCore Runtime network
interface(s) (type agentic_ai) are still attached to the Runtime's security group.
AgentCore removes them up to 8 hours after the Runtime is deleted, and they cannot be
deleted by hand. The office and voice stacks are left as they are, because the LiveKit
host still holds the voice stack's Elastic IP.

Run this script again later (it is safe to re-run; it picks up where it stopped):
  infrastructure/scripts/cleanup.sh --yes
EOF
  fi
  exit 1
fi
echo "== 3/4 destroying OutboundIvrOfficeStack"
destroy_office
echo "== 4/4 destroying OutboundIvrVoiceStack"
destroy_voice

print_survivors
echo
echo "done"
