// Allow record_item, but only for a key from the prior-auth item keys any seeded case
// uses (scripts/seed_cases.py). See policies/README.md: Cedar can't look up a specific case's
// missing_items (no DynamoDB access from a policy), so this is the static half of the
// rule; the per-case check (is `key` actually missing on *this* case_id) is enforced in
// lambdas/tools/handler.py via outbound_ivr.tools.business.record_item.
permit(
  principal,
  action == AgentCore::Action::"OutboundIvrTools___record_item",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
) when {
  context.input has key &&
  [
    "bmi",
    "chest_pain_pattern",
    "conservative_care",
    "diagnosis",
    "ecg_result",
    "epworth_score",
    "exam_findings",
    "family_history",
    "functional_goals",
    "functional_limits",
    "headache_history",
    "injections",
    "injury_date",
    "injury_mechanism",
    "last_colonoscopy",
    "mri_date",
    "mri_findings",
    "neuro_exam",
    "pain_duration",
    "physical_therapy",
    "prior_imaging",
    "prior_visits",
    "progress_note",
    "red_flags",
    "risk_factors",
    "smoking_history",
    "symptom_duration",
    "symptom_onset",
    "symptoms",
    "therapy_outcome",
    "therapy_weeks",
    "witnessed_apneas",
    "xray_findings"
  ].contains(context.input.key) &&
  context.input has case_id &&
  context.input.case_id != ""
};
