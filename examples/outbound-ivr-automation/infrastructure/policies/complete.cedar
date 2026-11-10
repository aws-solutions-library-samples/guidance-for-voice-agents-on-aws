// complete is allowed unconditionally at the Policy layer: whether all of this case's
// items are actually recorded is a per-case fact Cedar can't see (same limitation as
// record_item). outbound_ivr.tools.business.complete in the Lambda enforces the real rule --
// a case with items still missing is escalated instead of completed, regardless of
// what the model claims in `summary`. This policy exists so `complete` is explicitly
// permitted rather than falling through to the deny-everything-else default.
permit(
  principal,
  action == AgentCore::Action::"OutboundIvrTools___complete",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
) when {
  context.input has case_id &&
  context.input.case_id != ""
};
