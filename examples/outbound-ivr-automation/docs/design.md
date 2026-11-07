# Outbound IVR: design and contract

Change this file first, then the code. Diagrams: `docs/diagrams/`.

This document covers the agent's behavior and the data contract between the agent, the
Lambda tools, and the dashboard: self-hosted LiveKit on ECS, the AgentCore Runtime, the
Gateway and policy engine. For the infrastructure itself, read the CDK stacks under
`infrastructure/stacks/` and the root README's "Deploy" section.

## Goal

Outbound IVR, a Strands Bidi agent on Amazon Nova 2.5 Sonic, places an outbound call to a mock
provider office, navigates its phone menu by pressing real DTMF tones, is transferred to
the front desk, collects the three missing prior-auth items, and escalates (or hands off
to a colleague) if refused. Each call's evidence (items, outcome, transcript, tool
calls, recording) is stored and shown in the dashboard. All data is synthetic.

## Agent behaviour

Code in `src/outbound_ivr/voice/`.

- Model: `BedrockNovaSonicModel`, `model_id="amazon.nova-2-5-sonic"`, region us-east-1.
  The deployed Runtime sets `OUTBOUND_IVR_VOICE`; the default voice is `amy`. Strands' own
  default is `matthew`, a polyglot voice that drifts into other languages on short
  utterances.
- **navigate** phase: `prompts.ivr_prompt(NAVIGATE_GOAL)`, endpointing `LOW`. The agent
  does not speak. Tools: `press_key(digit, option)`, `menu_status(status)`.
- **intake** phase: `prompts.prior_auth_prompt()`, endpointing `HIGH` by default
  (`MEDIUM` if Outbound IVR cuts in on someone who pauses mid-sentence). Opens with
  `prompts.opening_line()` verbatim (it carries the automated-assistant disclosure).
  Tools: `record_item`, `escalate`, `complete`, `transfer_to_team`, applying
  `outbound_ivr.intake` rules, including "`complete` with items missing becomes escalated".
  After a transfer, the agent waits until the person has said something, then opens; if
  the person already greeted during the transfer message (heard by the menu phase), it
  opens at once.
- After `complete` or `escalate` the agent says goodbye; once that audio has played and
  nothing new comes for a few seconds it hangs up the call. `transfer_to_team` instead
  leaves the call running for the colleague who joined. See
  `docs/escalation-handoff.md`.
- Each phase runs `BidiAgent.run()` with the transport's own input and output streams. A
  tool can't end its own session, so `finish_phase` makes the input stream raise
  `_PhaseEnd`; `run()` then stops the streams (playback finishes first) and closes the
  session. Strands restarts the Nova Sonic connection at 7 minutes (its limit is 8) and
  replays the history; each phase is its own session.
- Audio out is paced to real time (Nova produces it faster than real time). If no audio
  arrives from the far end for a while, the agent feeds Nova Sonic silence, so the
  session doesn't hit Bedrock's 55-second no-audio timeout.
- A `ToolAudit` hook (Strands `AfterToolCallEvent`) records every tool call in the case
  record (`tool_calls`: time, phase, tool, input, status, result, duration) and as a
  `tool` event.
- Navigate moves to intake when the model calls `menu_status("completed")`. Nova Sonic
  fixes its instructions at session start, so a phase change means a new session.
- `press_key` is gated by `navigate.gate_keypress(option, heard)` against the far end's
  transcript for the current menu. Rejected: no tone, a `status: wait` event, and a tool
  result telling the model the option was never offered.
- Repeated digits are separate presses: 2 then 2 sends two tones.
- The menu prompt (`prompts.NAVIGATION_PROMPT`, via `ivr_prompt()`) is written for the
  `press_key` and `menu_status` tools and never mentions text tags such as `<dtmf>` or
  `<ivr>`: a prompt that describes those tags instead of calling real tools causes Nova
  Sonic to speak the tags aloud.

## Events and evidence

The agent writes one event per occurrence (`state.log.write(type_, **fields)`),
forwarded to DynamoDB (`OUTBOUND_IVR_EVENTS_TABLE`) for the dashboard API to read, and printed
to stdout for CloudWatch Logs:

```json
{"ts": "2026-10-08T15:00:01.123Z", "call": "<uuid>", "type": "dtmf", "digit": "2", "option": "2 - providers"}
```

| type | fields |
|---|---|
| `ready` | `mode`, `model`, `case`, `checklist` |
| `phase` | `phase`: `navigate` or `intake` |
| `heard` | `text`: caller's speech, transcribed |
| `said` | `text`: the agent's speech |
| `dtmf` | `digit`, `option` |
| `status` | `value`: `wait`, `completed`, `stuck` |
| `got` | `key`, `value`, `confirmed` |
| `escalate` | `reason`, `follow_up` |
| `handoff` | `outcome`: `complete`, `escalated` or `handed_off`, plus tool fields |
| `tool` | `tool`, `status`, `ms`, `input` (every tool call, from the audit hook) |
| `ended` | `reason`: `agent hung up`, `far end hung up` or `handed off` |
| `error` | `text` |

When the call ends, the agent writes the case record (S3
`outbound-ivr-artifacts/cases/<case_id>/<call_uuid>.json` in the deployed Runtime) in the
`intake.new_record()` shape, with the transcript and outcome filled in. **This is the
stored evidence**: the three items, their values, when each was confirmed, and who said
what. `OUTBOUND_IVR_TRACE_CALL_CONTENT` (see the root README's "Security and data") controls
whether that transcript also reaches traces and the online evaluators.

## Tools

`src/outbound_ivr/tools/` holds the business tools as plain functions over a storage interface
(cases, offices and events in DynamoDB; artifacts in S3): `record_item(case_id, call,
key, value, confirmed)`, `escalate(case_id, call, reason, follow_up)`,
`complete(case_id, call, summary)`, `transfer_to_team(case_id, call, reason, summary)`,
applying the `outbound_ivr.intake` rules. The same functions back the Gateway tools Lambda
(`src/lambdas/tools/`) and, with `OUTBOUND_IVR_TOOLS=local`, an agent's in-process tools; the
deployed Runtime always uses `OUTBOUND_IVR_TOOLS=gateway`. The agent's Strands tools keep their
current names and arguments either way (the model sees no difference); with `gateway`
they call the Gateway's MCP tools through a Strands `MCPClient`.

**Policy, on the Gateway:** `record_item` only for the case's missing-item keys;
`escalate`/`transfer_to_team` must carry a non-empty reason; `complete` only when the
case has all items recorded (checked in the Lambda as well, so the rule holds without
the Gateway); `transfer_to_team` capped at once per call; everything else denied. The
deployed engine enforces `infrastructure/policies/dogwood/*.dw`; `infrastructure/policies/*.cedar`
is the same contract in Cedar, used only with `-c policyLanguage=cedar`. See
`infrastructure/policies/README.md`.

**Evaluations:** an online evaluation config on the Runtime's traces with built-in
evaluators (helpfulness, faithfulness, tool selection) at a sampling rate set in CDK
(`evaluationSamplingPercentage`).

## Data (DynamoDB, not a relational store)

Every access pattern is by key: get a case, update it, list pending cases through a GSI
on status, look up an office. That shape is why this is DynamoDB rather than a
relational store.

| Data | Where |
|---|---|
| Cases and their missing items | DynamoDB `outbound-ivr-cases` (key `case_id`; GSI on `status`) |
| Provider offices and numbers | DynamoDB `outbound-ivr-offices` (key `office_id`) |
| Evidence (items, outcome, reason) | the case item, written by the tool Lambdas |
| Case record (transcript, tool calls) | S3 `outbound-ivr-artifacts/cases/<case_id>/<call>.json`, written by the agent role |
| Recordings | S3 `outbound-ivr-artifacts/recordings/<call>.wav` |
| Live events | DynamoDB `outbound-ivr-events` (key `call`, sort key `seq`, TTL'd), read by the dashboard API |
| Spans | AgentCore Observability (CloudWatch, X-Ray) |

## Dashboard

CloudFront + S3 single-page app, API Gateway (HTTP API) + one Lambda over DynamoDB and
S3, Cognito sign-in, a case queue view, live events by polling. See
`src/lambdas/api/README.md` for routes, IAM per route, and the `outbound-ivr-events` item shape.

## Ground rules

- Treat `src/outbound_ivr/prompts.py`, `intake.py` and `navigate.py` as shared contracts: change
  them deliberately, record the change in this document, and retest navigation and intake
  on a live call.
- Synthetic data only.
