// Outbound IVR is AWS-only: the dashboard always talks to the deployed API Gateway + Cognito
// stack (lambdas/api/). `isAws` is kept as a named export (rather than inlining `true`
// at every call site) purely so the aws-specific code it still guards -- gated behind
// config.json features like Connect CCP embedding and the LiveKit monitor -- reads as
// deliberate at each call site, not a leftover. `npm run build` and `npm run build:aws`
// are equivalent; the latter name is kept because scripts/deploy.sh calls it.
export const isAws = true
