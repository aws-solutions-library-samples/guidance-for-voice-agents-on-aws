#!/usr/bin/env bash
# Push the mock provider IVR flow to Connect, filling in values from .env and SSM. This
# is an ad hoc convenience only: stacks/office_stack.py's
# OutboundIvrOfficeStack creates this same flow directly inside the Connect instance it owns,
# substituting the real queue arn at deploy time, so a normal `scripts/deploy_office.sh`
# already pushes current content (OutboundIvrStack and `scripts/deploy.sh` have no involvement
# in the office flow at all now -- see office_stack.py's own module docstring). Use this
# script when you only changed connect/flows/ (via connect/make_flow.py) and want it live
# without a full `cdk deploy OutboundIvrOfficeStack`.
#   scripts/deploy_flow.sh            update the live flow
#   scripts/deploy_flow.sh --dry-run  print the rendered flow, change nothing
#
# The committed flow carries one placeholder, ${FRONT_DESK_QUEUE_ARN}, filled below from
# SSM /outbound-ivr/connect/front-desk-queue-arn (published by OutboundIvrOfficeStack).
set -euo pipefail
cd "$(dirname "$0")/../.."

[ -f .env ] || { echo "missing .env: copy .env.example to .env and fill it in" >&2; exit 1; }
set -a; source .env; set +a
: "${MOCK_PROVIDER_FLOW_ID:?set MOCK_PROVIDER_FLOW_ID in .env}"
# Same Region convention as scripts/deploy.sh/deploy_office.sh: no fallback.
: "${OUTBOUND_IVR_REGION:?set OUTBOUND_IVR_REGION in .env: the Region the office stack is deployed to}"
REGION="$OUTBOUND_IVR_REGION"

src=infrastructure/connect/flows/outbound-ivr-mock-provider-ivr.json
rendered=$(cat "$src")

if grep -q '\${FRONT_DESK_QUEUE_ARN}' <<<"$rendered"; then
  # Read-only: this is the ARN OutboundIvrOfficeStack already published, not a secret.
  queue_arn=$(aws ssm get-parameter \
    --region "$REGION" \
    --name /outbound-ivr/connect/front-desk-queue-arn \
    --query "Parameter.Value" --output text)
  # "#" delimiter: a queue ARN contains "/", which "s/.../.../" would misparse.
  rendered=$(sed "s#\${FRONT_DESK_QUEUE_ARN}#${queue_arn}#g" <<<"$rendered")
fi

if grep -q '\${' <<<"$rendered"; then
  echo "unfilled placeholder left in $src" >&2; exit 1
fi

if [ "${1:-}" = "--dry-run" ]; then echo "$rendered"; exit 0; fi

# The instance id is no longer a .env value: OutboundIvrOfficeStack (stacks/office_stack.py)
# creates and owns the Connect instance, and publishes its id to this read-only SSM
# parameter -- not a secret, just not known until after a first deploy. Resolved here,
# not above, so --dry-run never needs it.
CONNECT_INSTANCE_ID=$(aws ssm get-parameter \
  --region "$REGION" \
  --name /outbound-ivr/connect/instance-id \
  --query "Parameter.Value" --output text)

echo "updating flow $MOCK_PROVIDER_FLOW_ID on instance $CONNECT_INSTANCE_ID"
aws connect update-contact-flow-content \
  --region "$REGION" \
  --instance-id "$CONNECT_INSTANCE_ID" \
  --contact-flow-id "$MOCK_PROVIDER_FLOW_ID" \
  --content "$rendered"
echo "done"
