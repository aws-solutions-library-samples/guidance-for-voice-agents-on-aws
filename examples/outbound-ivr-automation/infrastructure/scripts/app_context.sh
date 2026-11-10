# Shared CDK context for deploy.sh, deploy_office.sh and cleanup.sh. Source it after
# .env is loaded and ROOT (the example folder) is set.
#
# infrastructure/app.py builds all three stacks on every cdk command, whichever stack
# the command targets, so every command needs the context all three stacks read.
# Keeping one list here means a deploy, a synth and a destroy always see the same app.

app_ctx=(
  -c "agentEmail=${AGENT_EMAIL:?set AGENT_EMAIL in .env}"
  -c claimOfficeNumber=true
  -c claimEvidenceNumber=true
  -c "dashboardDistDir=$ROOT/src/dashboard/dist"
)
# The colleague's phone the agent escalates to in "direct" evidence mode.
[ -n "${FRONT_DESK_NUMBER:-}" ] && app_ctx+=(-c "testCellNumber=$FRONT_DESK_NUMBER")
# "direct" dials testCellNumber straight into the call; unset or "connect" uses the
# handoff code into the Connect evidence queue.
[ -n "${EVIDENCE_MODE:-}" ] && app_ctx+=(-c "evidenceMode=$EVIDENCE_MODE")
# Where the operational alarms send email. Without it the alarms exist but notify nobody.
[ -n "${ALERT_EMAIL:-}" ] && app_ctx+=(-c "alertEmail=$ALERT_EMAIL")
# The Chime number outbound calls present as caller ID (ordered once, outside CDK).
[ -n "${CALLER_ID_NUMBER:-}" ] && app_ctx+=(-c "callerIdNumber=$CALLER_ID_NUMBER")

build_dashboard() {
  echo "building the dashboard SPA (npm --prefix src/dashboard run build:aws)"
  npm --prefix "$ROOT/src/dashboard" ci --no-fund --no-audit
  npm --prefix "$ROOT/src/dashboard" run build:aws
}

# OutboundIvrStack bundles the dashboard build as an asset, so the app cannot even be
# synthesized (for any stack) until it exists once.
ensure_dashboard_built() {
  [ -f "$ROOT/src/dashboard/dist/index.html" ] || build_dashboard
}

cdk_app() {
  (cd "$ROOT/infrastructure" && uv sync -q --inexact --extra infra --extra dev && cdk "$@" "${app_ctx[@]}")
}
