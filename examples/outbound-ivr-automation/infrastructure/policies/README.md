# AgentCore Policy for Outbound IVR's tools: Dogwood (default) and Cedar (fallback)

Two statement sets for the same four tools (`record_item`, `escalate`, `complete`,
`transfer_to_team`), gating an AgentCore Policy engine in `PolicyEngineMode.ENFORCE`. The
deployed engine is `outbound_ivr_policy_engine`, attached to the `outbound-ivr-tools`
Gateway by `infrastructure/stacks/gateway_stack.py`, the only `outbound-ivr-tools`
Gateway/Policy definition in this repo today. It reads these files:

- `infrastructure/policies/dogwood/*.dw`: [Dogwood](https://aws.amazon.com/blogs/opensource/introducing-the-dogwood-local-engine-temporal-governance-for-agent-actions/),
  the open-source policy language AgentCore Policy uses for session-aware
  authorization. **Default** (`-c policyLanguage=dogwood`, or no `policyLanguage`
  context at all).
- `infrastructure/policies/*.cedar`: the original [Cedar](https://www.cedarpolicy.com/en/tutorial)
  statements. Kept as a fallback: pass `-c policyLanguage=cedar` to CDK to create
  Policies from these instead. Dogwood is a superset of Cedar ("every valid Cedar
  policy is also a valid Dogwood policy" (see
  [Core concepts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-core-concepts.html)),
  so the two statement sets express the same point-in-time rules; only Dogwood adds the
  two temporal (session-aware) rules below.

`infrastructure/stacks/gateway_stack.py` reads whichever directory `policyLanguage` selects and
creates one `AWS::BedrockAgentCore::Policy` per file, each a separate CDK resource
(`PolicyRecordItem`, `PolicyEscalate`, ...). Dogwood statements go into the Policy
definition's **`policy`** member; Cedar statements go into its **`cedar`** member:
these are the two members of the `PolicyDefinition` union in the
[`CreatePolicy`](https://docs.aws.amazon.com/aws-sdk-php/v3/api/api-bedrock-agentcore-control-2023-06-05.html#shape-policydefinition)
API (`CfnPolicy.PolicyDefinitionProperty.policy` / `.cedar` in CDK's L1 construct for
this resource, in `aws-cdk-lib` 2.273.0; see "Verified from the docs" below for why
the L1 escape hatch is used instead of the alpha module's `agentcore.Policy` L2
construct). Both go through the same `PolicyEngine`/`Gateway` and the same action-name
convention, `"{TargetName}___{tool}"` (three underscores, e.g. `OutboundIvrTools___record_item`),
the prefix the Lambda strips from `bedrockAgentCoreToolName` in
`src/lambdas/tools/handler.py`. Replace `${GATEWAY_ARN}` with the deployed Gateway's ARN
(CDK does this at synth time by substituting `gateway.gateway_arn` into the statement
text).

`transfer_to_team` is only available on the deployed LiveKit transport: see
`docs/escalation-handoff.md`. It is not part of the rule table below; that table
documents `record_item`/`escalate`/`complete`, which `transfer_to_team` doesn't change.
`infrastructure/stacks/gateway_stack.py` defines `outbound-ivr-tools` and
`outbound_ivr_policy_engine`, the Gateway and Policy engine this repo actually deploys and
enforces; these statement files are what it deploys.

## What each file does

| File (both dirs, by stem) | Rule | Statement kind |
|---|---|---|
| `record_item` | `key` in the demo's static item-key allowlist, `case_id` non-empty | `permit` + `when` |
| `escalate` | `reason` and `case_id` both non-empty | `permit` + `when` |
| `complete` | `case_id` non-empty (permitted unconditionally otherwise) | `permit` + `when` |
| `transfer_to_team` | `reason` and `case_id` both non-empty | `permit` + `when` |
| `transfer_to_team_once` (Dogwood only) | forbids a second `transfer_to_team` call in the same session | `forbid` + `when temporal` |

`transfer_to_team_once.dw` has no Cedar counterpart: Cedar has no session history to
test against, so "at most once per session" cannot be expressed in Cedar at all. This
is the one rule Dogwood covers that Cedar structurally cannot; everything else is a
like-for-like port.

## The rules, and where each is actually enforced

| Rule (from the design doc) | Policy (Dogwood or Cedar) | Lambda (`outbound_ivr.tools.business`) / agent (`src/outbound_ivr/voice/agent.py`) |
|---|---|---|
| `record_item` only for the case's missing-item keys | `key` in a **static** allowlist of every item key the seeded cases use, `case_id` non-empty | the real check: `key` must be in *this* `case_id`'s `missing_items` (`business.record_item`) |
| `escalate` requires a non-empty reason | `reason != ""` | also rejects a whitespace-only reason (`"   "`), which Policy's `!=` can't catch without `trim()` (`business.escalate`) |
| `escalate`, LiveKit mode, only after a failed `transfer_to_team` attempt | **not enforced here**: see "Why the escalate-after-transfer_to_team rule stays Python-only" below | the real check, including the "no `team_dialer` at all" exception (`agent.py`'s `escalate` tool) |
| `complete` allowed, `complete` with items missing becomes escalated | permitted unconditionally (any `case_id`) | the real check: looks up the case's results, escalates if anything is unconfirmed, whatever `summary` claims (`business.complete`) |
| `transfer_to_team` requires a non-empty reason | `reason != ""`, `case_id != ""` | also rejects whitespace-only `reason`/`summary` (`business.transfer_to_team`, `agent.py`'s `transfer_to_team` tool) |
| `transfer_to_team` at most once per session | **Dogwood only**: `transfer_to_team_once.dw`, a `forbid` on the second request in a session (see "Updating the item-key allowlist" sibling section below for the temporal syntax) | `state.transfer_to_team_requested` guard in `agent.py` |
| everything else denied | **no policy needed**: `PolicyEngineMode.ENFORCE` denies any action with no matching `permit`, so there is no explicit "deny all else" rule to write | n/a |

Both layers matter: Policy is the first gate (blocks nonsense `key`s, empty `reason`s,
and (Dogwood only) a second `transfer_to_team` call, before the Lambda even runs
its DynamoDB calls); the Lambda and the agent are still the authority on the per-case
and per-call-state facts Policy cannot see. Policy failing open (`LOG_ONLY` mode, or no
engine attached) would still be safe, because the Lambda's and the agent's own rules
hold regardless: this is why `record_item`'s per-case check, `complete`'s
missing-items check, and `escalate`'s transfer-to-team-first check all stay enforced in
Python even though a Dogwood or Cedar policy also exists (or, for the last one,
deliberately does not).

## Why Policy only gets the static half of `record_item` and `complete`

Policy evaluates `when` conditions against `context.input` (the tool call's own
arguments) and `principal` attributes (from the OAuth token); it has no way to look
up a DynamoDB item by the `case_id` in that same input, so it cannot know *this
specific case's* `missing_items` or which keys are already confirmed. This is true of
both Cedar and Dogwood: Dogwood's only documented information provider beyond its own
recorded event history is Amazon Bedrock Guardrails
([Temporal policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-temporal.html)),
nothing for an arbitrary data lookup. Two options considered for `record_item`:

1. **(chosen)** Policy enforces what it can see without a lookup: a fixed, demo-wide
   allowlist of item keys for `record_item`, and that `reason`/`case_id` fields are
   non-empty. The per-case rules (is `key` actually missing *on this case*, is the
   case actually complete) are enforced in the Lambda, which does have DynamoDB
   access. This is what the design doc anticipates ("if per-case lookups aren't
   possible in Cedar, enforce the static set of allowed keys and the non-empty-reason
   rule in Cedar and the per-case rule in the Lambda") and it carries over unchanged to
   Dogwood for the same reason.
2. (rejected) Encode every case's allowed keys as its own policy, generated and
   deployed whenever a case is created. Rejected: couples case creation to a Policy
   deployment, works against "Tasks and sessions hold nothing that outlives a call",
   doesn't scale past the demo's three items, and (new with Dogwood) adding a policy
   invalidates every open temporal session on the engine (see below): creating one
   per case would do that on every new case, which is a bigger blast radius than Cedar
   ever had to worry about.

The same reasoning applies to `complete`: a temporal rule could confirm that *some*
`record_item` calls happened this session, or even that calls for a fixed, static set
of keys happened, but not that *this case's actual* `missing_items` (which varies per
case) was fully covered. Gating `complete` on the static three-key superset would
incorrectly deny completion for any case whose `missing_items` is a strict subset of
that superset (the common case), so `complete.dw`/`complete.cedar` stay an
unconditional permit (`case_id` non-empty) and the Lambda remains the only place that
checks completeness for real.

## Why the escalate-after-transfer_to_team rule stays Python-only

`src/outbound_ivr/voice/agent.py`'s `escalate` tool, LiveKit mode only, refuses the call
("First bring in a colleague with transfer_to_team.") until `transfer_to_team` has been
tried **and failed** on this call, unless there is no `team_dialer` for this call to
try it with at all, in which case escalate is unaffected.

Dogwood can express "`transfer_to_team` was *attempted* this session" with a `formerly
within` temporal condition (see "Require a prerequisite action" in
[Temporal policy examples](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-temporal.html#example-policies-temporal-sequencing)).
It is not ported here, for two reasons:

1. It cannot express the "...unless there is no `team_dialer`" exception. Whether a
   `team_dialer` exists is an application-level fact, not an event the Gateway ever
   sees, and a Policy-layer rule here cannot make that distinction the way the Python
   check can.
2. Even restricted to LiveKit-only calls, `formerly within ...::request` matches that
   `transfer_to_team` was *attempted*, not that it *failed*: Policy has no visibility
   into the `TeamDialer`'s join/no-answer result, because that happens in-process
   (`agent.py`'s own code), not as a second Gateway-routed tool call Policy could see a
   `::response` or `::error` event for.

Net effect: this ordering rule is real and enforced, just not duplicated at the Policy
layer, for the same "Policy can't see what Python can" reason record_item's and
complete's per-case checks aren't duplicated either.

## Temporal policies: session IDs and session invalidation

Dogwood's temporal conditions (`transfer_to_team_once.dw`) are evaluated against a
*policy session*: callers send a session ID on the
`x-amzn-bedrock-agentcore-policy-session-id` header, starting with the first request; a
request with no session ID fails validation once the engine holds any temporal policy
([Temporal policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-temporal.html),
"Policy sessions and the session ID"). `src/outbound_ivr/voice/backend.py` sets it:
`GATEWAY_POLICY_SESSION_HEADER`, with one id per call from `_policy_session_id`.

**Creating or updating a temporal policy invalidates every active temporal session on
that engine.** Quoting the same doc: "If you change the engine's temporal policies
while a session is open, the recorded history no longer matches the current rules, so
the service ends the session instead of making a decision against inconsistent data.
Adding or updating a temporal policy on the engine invalidates the engine's active
temporal policy sessions. After such a change, the next request that reuses an
invalidated session fails with an HTTP 409 `ConflictException`." The fix on the client
side is to start a new session and retry: "the new session starts with an empty
history and is evaluated against your updated policies." Any in-flight multi-step
workflow (a call mid-handoff) restarts rather than resumes; this repo's caller does treat a 409 from
the Gateway as "generate a new session id and send the request again":
`src/outbound_ivr/voice/backend.py` raises `GatewayPolicySessionConflict` on a 409 and retries
once on a fresh session from `_fresh_policy_session_id`.

## Updating the item-key allowlist

`record_item.dw`/`record_item.cedar` list every item key the cases in
`infrastructure/scripts/seed_cases.py` use (the demo case's three plus the extra cases'). It's meant
to be a superset (the Lambda narrows it to the one case being worked), not the exact
set for any one case. When a case brings a new key, add it to both files'
`[...].contains(context.input.key)` list; `tests/infrastructure/test_policies.py` fails until you do.
Cedar's `in` tests entity membership only, so a string-in-list check has to be
`.contains()`: the policy engine rejects `"x" in ["x", "y"]` at create time.

## Verified from the docs, with quotes and URLs

- **Dogwood is Cedar-compatible, with temporal + Guardrails extensions.** "Dogwood
  extends the authorization model of Cedar, adding stateful, session-aware conditions
  and additional policy information providers, such as Amazon Bedrock Guardrails...
  Your existing point-in-time policies written in Dogwood or Cedar continue to work
  without changes." (see
  [Temporal policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-temporal.html)
- **File extension is `.dw`.** The authoring guide's example saves a Dogwood statement
  as `disburse_only_approved.dw` and creates it with `agentcore add policy ... --source
  disburse_only_approved.dw` (see
  [Authoring temporal policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-temporal-authoring.html)
- **The `PolicyDefinition` union has separate `cedar` and `policy` members.** "The
  Cedar policy definition within the policy definition structure... **policy**: The
  Dogwood policy statement that defines the access control rules." --
  [`CreatePolicy`/`PolicyDefinition`](https://docs.aws.amazon.com/aws-sdk-php/v3/api/api-bedrock-agentcore-control-2023-06-05.html#shape-policydefinition).
  Confirmed directly against the installed CDK: `aws-cdk-lib==2.273.0`'s
  `aws_cdk.aws_bedrockagentcore.CfnPolicy.PolicyDefinitionProperty.__init__` accepts
  both `cedar` and `policy` keyword arguments (the latter typed as
  `CfnPolicy.PolicyStatementProperty`, which itself is just `statement: str`): this
  is the L1 construct this stack already used before this change (see
  `gateway_stack.py`'s comment on why: "No L2 for attaching a policy engine to a
  Gateway yet").
- **The alpha L2 construct (`@aws-cdk/aws-bedrock-agentcore-alpha`) does not expose
  Dogwood.** Its own README states "Policies are constructed using [Cedar
  language]... For advanced Cedar features not supported by the builder, use raw Cedar
  strings," with no `fromDogwood` or `policy`-member equivalent documented anywhere in
  that module as of this writing. This is why this stack creates `Policy`/`PolicyEngine`
  via the L1 `CfnPolicy`/`CfnPolicyEngine` escape hatch rather than the alpha module's
  `agentcore.Policy`/`agentcore.PolicyEngine`: it already did, for the Gateway's own
  `PolicyEngineConfiguration` override, before this change; this change just reuses the
  same L1 pattern for the Dogwood `policy` member.
- **Temporal operators and quotas.** `formerly within`, `since within`, `count`, and
  `sum` are documented in
  [Temporal policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-temporal.html)
  and worked through in
  [Temporal policy examples](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-temporal.html).
  `transfer_to_team_once.dw` follows "Cap how many times an action runs" from the
  examples page verbatim in shape (`exists (n: Long). (count for (t: Timepoint). where
  (formerly within <window> (...::request{eventResource: resource} && tp(t)))) == n &&
  n > <limit>`), with `<limit>` set to 1 instead of 3. Quotas: "Temporal policies per
  policy engine: 20", "Temporal operators per policy: 3", "Maximum time window per
  temporal condition: 24 hours"; the 24h window used in `transfer_to_team_once.dw` is
  the maximum allowed, chosen because there is no "for the rest of the session,
  however long" option in the syntax and a session is expected to be much shorter than
  a day.
- **Session invalidation on policy change (the 409).** Quoted in full above, under
  "Temporal policies: session IDs and session invalidation", from the same
  [Temporal policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-temporal.html)
  page and from
  [Temporal policy examples](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-temporal.html#example-policies-temporal-invalidation)'s
  "What changes when you edit a temporal policy" section.
- **`AI-powered policy generation from natural language` exists but was not used
  here.** The
  [`StartPolicyGeneration`](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_StartPolicyGeneration.html)
  API and the "Authoring Dogwood policies from natural language" blog post describe a
  neuro-symbolic generation flow (LLM draft, then automated-reasoning validation); these
  `.dw` files were written directly against the documented syntax instead, so that
  every clause traces to a specific doc quote above rather than to an unreviewed
  generated draft.

## Open questions (not added: not clearly supported, or no basis in this repo)

- **Office-hours and per-office rate-limit rules.** Not added. Neither exists in
  `src/outbound_ivr/tools/business.py` or `src/outbound_ivr/voice/agent.py` today, and
  `src/lambdas/tools/schema.json` has no `office_id`-equivalent field on any of the four
  tools to key a per-office rule on in the first place (the per-case `office_id` lives
  in the case record, which Policy can't look up, same gap as `record_item`'s
  per-case check). The point-in-time `context.system.now.toTime()` pattern from the
  "Authoring Dogwood policies from natural language" blog post would let a policy
  express office hours *if* a specific hours-of-operation requirement and timezone were
  given, but nothing in this repo specifies one, so adding it would be guessing a
  business rule rather than documenting one.
- **Whether `agentcore.Policy`'s `fromDogwood`-equivalent lands in a future alpha
  release.** Not found as of the alpha module version resolved by this repo's
  `uv.lock` (`aws-cdk-lib` 2.273.0). If a future version adds one, the L1
  `CfnPolicy`/`PolicyStatementProperty` construction in `gateway_stack.py` could be
  simplified to use it, but there's nothing to confirm or guess at today.
