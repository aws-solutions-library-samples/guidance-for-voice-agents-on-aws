// transfer_to_team requires a non-empty reason, same rule as escalate.cedar. Also
// requires case_id non-empty, for the same reason escalate.cedar gives: Policy is the
// independent check, not a rubber stamp on what schema.json already marks required.
// Whitespace-only reasons ("   ") pass Cedar but are caught by
// outbound_ivr.tools.business.transfer_to_team in the Lambda, same as escalate's own check.
permit(
  principal,
  action == AgentCore::Action::"OutboundIvrTools___transfer_to_team",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
) when {
  context.input has reason &&
  context.input.reason != "" &&
  context.input has case_id &&
  context.input.case_id != ""
};
