// escalate requires a non-empty reason. Cedar has no trim(); this catches the empty
// string, which is what the Nova Sonic tool call will actually send if the model omits
// a reason (the field is required in schema.json, but Policy is the independent check).
// Whitespace-only reasons ("   ") pass Cedar but are caught by
// outbound_ivr.tools.business.escalate in the Lambda, same as record_item's per-case check.
permit(
  principal,
  action == AgentCore::Action::"OutboundIvrTools___escalate",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
) when {
  context.input has reason &&
  context.input.reason != "" &&
  context.input has case_id &&
  context.input.case_id != ""
};
