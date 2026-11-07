# `src/lambdas/api`: the dashboard's API Gateway Lambda

This is the deployed dashboard's one Lambda, behind API Gateway's HTTP API and a
Cognito JWT authorizer. CDK (`infrastructure/stacks/dashboard_stack.py`) wires the
HTTP API, the authorizer, this function's role and env vars, and the tables below.

See `docs/design.md`, "Dashboard".

## Routes

One Lambda (`handler.lambda_handler`) behind one HTTP API, routed on `routeKey`. Every
route sits behind the API's Cognito JWT authorizer; nothing in this handler re-checks
auth, so API Gateway must reject unauthenticated and unauthorized requests before the
Lambda runs.

| Route | What | IAM the Lambda's role needs |
|---|---|---|
| `GET /api/cases` | Scans `outbound-ivr-cases`; returns every item, newest `updated_at` first. | `dynamodb:Scan` on `outbound-ivr-cases` |
| `GET /api/cases/{id}` | The case item, plus its case records from S3 (`cases/{id}/*.json`, see below). 404 if the case doesn't exist. | `dynamodb:GetItem` on `outbound-ivr-cases`; `s3:ListBucket` on `outbound-ivr-artifacts` (prefix `cases/{id}/`); `s3:GetObject` on `outbound-ivr-artifacts/cases/*` |
| `PATCH /api/cases/{id}` `{"fields": {...}}` and/or `{"reset": true}` | Edit a case's info and/or reset a case stuck in `"queued"` (or any other non-final state). See "Editing/resetting a case" below. 400 on an unknown field, a non-string value, a value over 200 chars, or an invalid `status`. 404 if the case doesn't exist. 409 if the case is `"calling"` and `reset` isn't also `true`. Returns the updated case, same shape as one item of `GET /api/cases`. | `dynamodb:GetItem` and `dynamodb:UpdateItem` on `outbound-ivr-cases` |
| `GET /api/calls/{call}/events?after=<seq>` | Events for one call from `outbound-ivr-events`, with `seq` strictly greater than `after` (default `-1`, i.e. everything), ascending. The SPA polls this once a second, keeping the highest `seq` it has seen as the next `after`. | `dynamodb:Query` on `outbound-ivr-events` |
| `GET /api/calls/{call}/monitor?mode=listen\|levels&after=<seq>` | Polled listen-in: lists and reads new audio/levels segments for the call, and refreshes a watch marker that tells the Runtime someone is watching. 404 `{"error": "monitor not enabled"}` if `OUTBOUND_IVR_MONITOR_BUCKET` is unset; see "Monitor" below. | `s3:ListBucket` on the Monitor bucket (prefix `*/seg/*`); `s3:GetObject` on `*/seg/*` and `*/watch/*`; `s3:PutObject` on `*/watch/*` |
| `GET /api/recordings/{call}` | `{"url": "<presigned GET on recordings/{call}.wav>"}`, valid 5 minutes. 404 if the object doesn't exist. 503 if no artifacts bucket is configured. | `s3:GetObject` on `outbound-ivr-artifacts/recordings/*` (the presigned URL is generated with the Lambda's own credentials, so it needs the permission even though it never fetches the body) |
| `POST /api/calls` `{"case_id": "..."}` | Starts a call. 404 if the case doesn't exist. 409 if the case is already `"calling"` or `"queued"`, has hit `OUTBOUND_IVR_MAX_CALL_ATTEMPTS`, or was dialed inside `OUTBOUND_IVR_CALL_COOLDOWN_SECONDS`; the `detail` says which. 500 if `OUTBOUND_IVR_RUNTIME_ARN` is unset. See "Starting a call" below. | See "Starting a call" below |

### Monitor

`handle_get_monitor` returns `{"segments": [...], "after": <highest seq returned>}`,
or `404 {"error": "monitor not enabled"}` if `OUTBOUND_IVR_MONITOR_BUCKET` is unset (the
Monitor nested stack wasn't built for this deploy; see
`infrastructure/stacks/monitor_stack.py`). `call` must be a canonical lowercase uuid (400
otherwise); `mode` (query param, `listen` or `levels`, default `listen`; 400 on
anything else) picks which feed to read: `listen` gets the mixed `pcm` audio,
`levels` gets only the per-leg RMS pair for the speaking indicators (`pcm` is
stripped from every chunk in `levels` mode, even if the Runtime wrote it for
another, listening, viewer). `after` (query param, an int; 400 if present and not
one) skips to segments past that `seq`; missing `after` returns only the newest
segment, to start near live.

Each call refreshes a zero-byte watch marker under `<call>/watch/<mode>` in the
Monitor bucket: this is how the Runtime (`src/outbound_ivr/voice/monitor.py`) learns
someone is watching, without the Runtime ever talking to the dashboard directly.
A marker older than 15 seconds is audited and (re)written; between 5 and 15
seconds it's refreshed without an audit line; fresher than that, the write is
skipped. There is no ticket, no WebSocket, and no JWT beyond the one API Gateway's
own Cognito authorizer already checked; this route's only authorization is that
same authorizer. The dashboard polls it every 2 seconds, so listen-in audio lags
the live call by about 3-6 seconds (one segment's buffering plus the poll
interval).

### Starting a call

`handle_post_calls` invokes the Outbound IVR AgentCore Runtime directly (see
docs/escalation-handoff.md for the shared start-call contract it was written against).
404 if `case_id` doesn't name a known case. 500 `{"detail": "OUTBOUND_IVR_RUNTIME_ARN is not
configured"}` if the Runtime hasn't been deployed yet or the dashboard stack wasn't
redeployed after it was; there is no other path to fall back to.

1. Mint `call_id = str(uuid4())` here: the Runtime does not assign one.
2. `UpdateItem` on `outbound-ivr-cases`: `status = "queued"` (refusing with `409` if the case
   is already `"calling"`) and `current_call = <call_id>`, so a reader can resolve this
   case's in-flight call id.
3. Call `bedrock-agentcore:InvokeAgentRuntime` (boto3 service name
   `"bedrock-agentcore"`, method `invoke_agent_runtime`) with:
   - `agentRuntimeArn` = `OUTBOUND_IVR_RUNTIME_ARN`
   - `runtimeSessionId` = `f"{call_id}-call"`: AgentCore requires at least 33
     characters; `call_id` (a uuid4, 36 characters) already clears that on its own,
     but the suffix is the contract's own padding, applied unconditionally rather than
     only when needed
   - `contentType` = `"application/json"`
   - `payload` = UTF-8 JSON bytes of `{"action": "start_call", "call_id", "case_id"}`
     (nothing about the office number, trunk, or menu digits: the Runtime reads those
     from its own environment and navigates by listening, not from this payload
4. Return `202` `{"call": call_id}`, the real call id, not an echo of `case_id`. This
   is what the SPA uses for every subsequent read (`GET /api/calls/{call}/events`,
   etc.).

IAM: `dynamodb:GetItem` and `dynamodb:UpdateItem` on `outbound-ivr-cases`;
`bedrock-agentcore:InvokeAgentRuntime` scoped to the Runtime's ARN and its
`runtime-endpoint/*` ARNs (not wildcarded across all runtimes).

### Editing/resetting a case

`handle_patch_case` lets the dashboard fix a case's info by hand, and get a case
unstuck when it's wedged in `"queued"` (or any other non-final state) with no call
actually in flight.

Request body: `{"fields": {...}}` and/or `{"reset": true}`: at least one of the
two, or `400`.

**`fields`**: an object of `{field: "new value"}`. Editable fields: `status` (one
of `pending`, `queued`, `calling`, `complete`, `escalated`), `patient_name`,
`patient_dob`, `member_id`, `auth_ref`, `procedure`, `payer`, `ordering_provider` --
the string fields on `outbound_ivr.tools.store.default_case` that make sense to correct by
hand (not `case_id`, not the structured/derived `missing_items`, `results`,
`attempts`, `outcome`, `office_id`, `updated_at`). Every value must be a string,
trimmed, 200 characters or fewer; any other field name, or `status` outside the
five values above, is a `400`.

**`reset`**: `true` sets `status` back to `"pending"` and `attempts` to `0`, and
removes `outcome`, `current_call`, `last_call`, and `last_call_started_at`; the case
starts over clean, as if it had never been called. If `fields` also sets `status` in
the same request, `reset`'s own `"pending"` wins.

Clearing `attempts` and `last_call_started_at` is also what lifts `POST /api/calls`'s
attempt cap and dial cooldown, so reset is the documented way back from a case that
has run out of attempts or is inside its cooldown.

`updated_at` is always set to now, on either kind of edit.

**Guard:** if the case's current status is `"calling"`, plain `fields` edits are
refused with `409`: editing a case out from under a call in flight would be worse
than leaving it stuck. Pass `reset: true` (with or without `fields`) to get out of
`"calling"` as well.

404s if the case doesn't exist: checked both before the write (a `GetItem`) and on
the write itself (`ConditionExpression attribute_exists(case_id)`, in case the case
was deleted in between).

Returns `200` with the updated case item (decimals converted the same way `GET
/api/cases` converts them): the same shape as one item of that route's list, not
the `GET /api/cases/{id}` shape (which also carries `records` from S3; this route
never touches S3).

### `POST /api/calls/hangup`: not implemented here

The call runs inside one AgentCore Runtime container's own background task
(`src/agentcore/livekit_app.py`'s `_run_call`), started by this handler's `POST /api/calls`
and ending on its own when the far end hangs up, the agent completes or escalates, or a
handoff takes over. There is no callable surface on the Runtime today for an outside
caller to reach a specific in-flight call and end it early, and this Lambda has no path
to one. To implement it, the Runtime side would need to expose one, most plausibly
another AgentCore invoke action keyed by `call_id` that signals the right background
task, and this handler would then need that `call_id` from the request and permission
to call it. Nothing here currently supports it.

## Env vars

| Var | Default | Used by |
|---|---|---|
| `OUTBOUND_IVR_CASES_TABLE` | `outbound-ivr-cases` | all routes but `/api/calls/{call}/events`, `/api/calls/{call}/monitor` and `/api/recordings/{call}` |
| `OUTBOUND_IVR_OFFICES_TABLE` | `outbound-ivr-offices` | passed to `DynamoDBCaseStore`'s constructor; not read by any route this handler actually serves today |
| `OUTBOUND_IVR_EVENTS_TABLE` | `outbound-ivr-events` | `GET /api/calls/{call}/events` |
| `OUTBOUND_IVR_ARTIFACTS_BUCKET` | *(none)* | `GET /api/recordings/{call}` (503 if unset); `GET /api/cases/{id}` (silently omits `records` if unset) |
| `OUTBOUND_IVR_RUNTIME_ARN` | *(unset)* | `POST /api/calls`: required; 500 if unset, there is no other path |
| `OUTBOUND_IVR_MAX_CALL_ATTEMPTS` | `5` | `POST /api/calls`: how many completed calls one case may have before it must be reset. `attempts` is incremented by the Runtime when it records an outcome, not at dial time |
| `OUTBOUND_IVR_CALL_COOLDOWN_SECONDS` | `60` | `POST /api/calls`: minimum gap between dials of the same case, measured from `last_call_started_at`. Bounds dial rate even when no call completes |
| `OUTBOUND_IVR_MONITOR_BUCKET` | *(none, 404 `{"error": "monitor not enabled"}` if unset)* | `GET /api/calls/{call}/monitor`: gates the route; the S3 bucket segments and watch markers are read from and written to |
| `OUTBOUND_IVR_DYNAMODB_ENDPOINT` | *(unset)* | local/test only: points `DynamoDBCaseStore` and the raw table resources at DynamoDB Local |

Region is **not** a configurable env var here: `os.environ["AWS_REGION"]` is read directly,
and Lambda always sets `AWS_REGION` to the function's own deployment region, so this always
talks to the tables in the region this function actually runs in.

## `outbound-ivr-events`: who writes it, and the item shape

**Something other than this Lambda writes this table.** This Lambda only reads it via
`GET /api/calls/{call}/events`. The Outbound IVR Runtime container writes directly, one
`PutItem` per event the agent logs (`outbound_ivr.voice.agent.EventLog`'s `forward` callback).
The writer (env var `OUTBOUND_IVR_EVENTS_TABLE`, default `outbound-ivr-events`) `PutItem`s one row per
event, shaped exactly like this:

```json
{
  "call": "<call uuid>",
  "seq": 17,
  "t": "2026-10-08T15:00:01.123Z",
  "type": "heard",
  "text": "...",
  "expires_at": 1760000000
}
```

- **`call`** (String): partition key. The call UUID.
- **`seq`** (Number): sort key. A per-call counter the Runtime keeps in memory, starting
  at `0` and incremented once per event it writes (one Runtime container per call, so no
  coordination needed). This is what `after=<seq>` in the GET route filters on, not a
  timestamp, so that polling never misses or double-delivers an event even if two land
  in the same millisecond.
- **`t`** (String): ISO 8601 UTC, the agent's own event timestamp.
- **`type`** and the rest of the fields: copy the inner event object's fields verbatim
  (`type`, plus whatever that type carries: `text`, `digit`, `option`, `value`,
  `confirmed`, `reason`, `follow_up`, `outcome`, `summary`, …). These are exactly the
  per-type fields documented in `docs/design.md`'s "Events and
  evidence" table (`ready`, `phase`, `heard`, `said`, `dtmf`, `status`, `got`,
  `escalate`, `handoff`, `tool`, `ended`, `error`): this handler and the SPA's
  `AgentEvent` type (`src/dashboard/src/lib/events.ts`) both already understand that shape,
  so writing it unchanged (plus `call`, `seq`, `expires_at`) is the whole job.
- **`expires_at`** (Number, epoch seconds): the table's TTL attribute, `now + 7 days`.
  CDK configures this table's native TTL on this attribute; DynamoDB deletes expired
  items on its own, no Lambda needed.

## S3 case records (`GET /api/cases/{id}`)

This read side assumes one JSON object per call attempt, keyed
`cases/<case_id>/<call_id>.json`, confirmed by the shared start-call contract as
`s3://OUTBOUND_IVR_ARTIFACTS_BUCKET/cases/{case_id}/{call_id}.json`, the same shape
`intake.new_record()` produces locally (`runs/cases/*.json`). A case attempted more
than once has multiple records, which this route returns oldest-first as
`case["records"]`. Writing it is gated on `OUTBOUND_IVR_ARTIFACTS_BUCKET` being set, same as
the events table above, whichever component runs the call (the media bridge, or the
Outbound IVR Runtime container directly) is the one that writes this key.

## Packaging

Same approach as `src/lambdas/tools/` (see its README): import `outbound_ivr.tools`, don't vendor
it. `handler.py` imports `outbound_ivr.tools.store.DynamoDBCaseStore`, i.e. the real package in
`src/outbound_ivr/tools/`, so case/office reads share one code path with the Gateway Lambda
target and the agent's local tools. Bundle by copying `src/outbound_ivr/tools/`,
`src/outbound_ivr/__init__.py` (just enough for `outbound_ivr.tools` to import) alongside
`handler.py` into the Lambda asset, the same way `src/lambdas/README.md` describes
for `src/lambdas/tools/_build/`.

**One real dependency now: `requirements.txt`, pinning `boto3`.** The AgentCore path
needs a boto3/botocore new enough to know the `bedrock-agentcore` service (newer than
what AWS Lambda's base runtime image bundles); `src/lambdas/tools/` needs no such pin
because it only uses services (DynamoDB, S3) the base image has always known. CDK's
asset bundling must `pip install -r requirements.txt --target <asset dir>` in addition
to copying `handler.py` and `outbound_ivr.tools` in: `infrastructure/stacks/lambda_bundling.py` has no
install step today, so this is new wiring on the CDK side, not just a file copy.

The monitor route adds no new dependency either: it's the same `s3` client
`handle_get_recordings` already uses for presigned URLs.
