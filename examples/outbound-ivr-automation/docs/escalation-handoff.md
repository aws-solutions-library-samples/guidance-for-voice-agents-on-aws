# Warm handoff to a human: the contract (LiveKit mode)

This is the contract for `transfer_to_team`, the warm handoff Outbound IVR can use when it is
stuck, always available on the deployed LiveKit transport. `escalate` (the Cedar/Dogwood
rule and the Lambda handler in `src/lambdas/tools/handler.py`) is unchanged and still the
right tool when nobody is coming to the call.

Change this file first, then the code, same convention as `docs/design.md`.

## Why a second escalation path

`escalate` (`outbound_ivr.tools.business.escalate`, bound in `outbound_ivr.voice.agent.make_intake_tools`)
ends the call: the office is told the review team will follow up later, and nobody comes
on the line now. That is the right outcome when there is nothing left to do on this call.
It is the wrong outcome when a person who can actually help is one SIP leg or one browser
tab away, in a demo where that colleague is a LiveKit room participant rather than a phone
transfer. `transfer_to_team` brings that person into the live call instead of ending it.

## 1. The intake tool: `transfer_to_team(reason, summary)`

Added to `make_intake_tools`'s result, alongside `record_item`, `escalate`, and
`complete`, unconditionally: the deployed transport is always LiveKit
(`state.transport` is a `LiveKitTransport`).

**When the model should use it:** a hold or a transfer *on the
front desk's own side* is cooperation, not a trigger for this tool: being told "hold
on, let me transfer you," or "let me go get someone," is them cooperating, and Outbound IVR
should accept briefly, say nothing while it waits, and re-introduce itself once a new
person is on the line, the same continuous phone call throughout. That is different from
Outbound IVR needing a colleague of its own: `transfer_to_team` is only for a real dead
end: the front desk cannot or will not give one of the missing items, and offers no
workable alternative at all (no hold, no transfer, no fax number, no portal, no "call
back Monday"), or the front desk asks to speak with someone on *Outbound IVR's own side*, a
colleague or supervisor of the payer's, not one of the office's own staff. `escalate`
stays the right tool for "I have to go" or "this isn't a case I can discuss", where no
one is coming to the phone this call. The dividing line for the model's own judgment: if
the next useful step is a person joining this call *from Outbound IVR's side*, call
`transfer_to_team`; if the next useful step is this call ending and someone following up
later, call `escalate`; if the next useful step is someone on the *office's* side coming
to the same phone, that's neither: just hold and continue.

**What Outbound IVR says first:** before calling the tool, Outbound IVR says, word for word, one line:

```
Let me bring in a colleague who can help with that.
```

This is a fixed line, the same pattern as `TRANSFER_NOTE`'s quoted opening in
`outbound_ivr/voice/agent.py`: it is not generated, so it never gets the sequencing wrong (it
must always come before the tool call, never after). The system prompt states the line
and the rule "say this, then call `transfer_to_team`" explicitly, the same way
the navigation prompt states the press_key/menu_status rule for the navigate phase.

**Arguments:**

| Arg | Meaning |
|---|---|
| `reason` | Why the call needs a person: one short phrase, e.g. "front desk cannot release records without a supervisor". Required, non-empty, same rule as `escalate`'s `reason`. |
| `summary` | One or two sentences a colleague can act on without re-asking everything: what the case needs, what has been collected so far, and what's still missing. This is also what Outbound IVR will say almost word for word once the colleague joins (see "What happens on join" below), so it should read naturally spoken, not as a case-note fragment. |

**It goes through the tool backend like the others.** `IntakeBackend` (`outbound_ivr/voice/backend.py`)
gets a fourth method:

```python
async def transfer_to_team(
    self, *, case_id: str | None, call: str, reason: str, summary: str
) -> dict[str, Any]: ...
```

- `LocalBackend.transfer_to_team` calls a new `outbound_ivr.tools.business.transfer_to_team`,
  the same shape as `escalate`: validates `reason` is non-empty, loads the case, and
  records an outcome-in-progress (`{"type": "transfer_to_team", "reason": ..., "summary": ...}`,
  status `"handoff_requested"`, not yet a final outcome the way `escalate`/`complete` set
  one: the call isn't over, so `store.set_outcome`'s "already has a final outcome" guard
  must not fire here or block a later `complete`/`escalate` on the same case if the
  handoff fails). It also writes `state.record`'s own bookkeeping, same as `escalate`
  does via `outbound_ivr.intake`.
- `GatewayBackend.transfer_to_team` forwards to a new Gateway MCP tool, `transfer_to_team`,
  behind the "outbound-ivr-tools" Lambda target (`src/lambdas/tools/handler.py` gets a fourth
  entry in `TOOL_HANDLERS`, `src/lambdas/tools/schema.json` gets a fourth tool definition,
  `case_id`, `call`, `reason`, `summary`, all required except nothing is optional here,
  unlike `escalate`'s `follow_up`).
- **Policy rule**: `reason` non-empty, and (following `escalate`'s own pattern
  exactly) `case_id` non-empty too, for the same reason that file gives: Policy is the
  independent check, not a rubber stamp on what `schema.json` already marks required.
  The statement exists in both languages: `infrastructure/policies/dogwood/transfer_to_team.dw`,
  which is what the deployed engine enforces, and
  `infrastructure/policies/transfer_to_team.cedar`, used only with `-c policyLanguage=cedar`.
  A second deployed rule, `infrastructure/policies/dogwood/transfer_to_team_once.dw`, forbids
  more than one `transfer_to_team` per policy session, so a call cannot hand off twice.

Calling `transfer_to_team` does **not** by itself ring anyone. Recording the request
(above) and reaching a human (below) are two separate steps, same separation of concerns
`IntakeBackend` (what to record) already has from `Transport` (what actually happens on
the call) elsewhere in this codebase.

## 2. `TeamDialer`: how the human is reached

A new interface, parallel to `IntakeBackend` and `TokenProvider` in `backend.py`, not a
subclass of either: it answers "how do we get a person onto this call", which is a
different axis from "where do we record that we asked". `AwsTeamDialer` is the only
implementation, and `handle_call` (`outbound_ivr/voice/agent.py`) always constructs one,
whatever transport the call is running on; see `AwsTeamDialer` below.

```python
class TeamDialer(ABC):
    @abstractmethod
    async def request_handoff(
        self, *, room: str, summary: str, reason: str, items: dict[str, Any]
    ) -> HandoffResult: ...
```

`items` is `state.record["items"]` as it stands at the moment of the call, the same
shape `outbound_ivr.intake.record_item` has been building all along, so whoever is brought in
gets exactly what's already in hand without a second serialization format.

### AwsTeamDialer

Not a LiveKit room participant at all on this leg: a real phone/queue, built by
`infrastructure/stacks/escalation_stack.py` and selected on the real Outbound IVR Runtime by
`OUTBOUND_IVR_TEAM_DIALER=aws` (`agent_runtime_stack.py` sets it unconditionally, since this
is the only team dialer the deployed Runtime uses).

> **Which of the two modes is live matters, and it is not this one.** Everything in this
> section describes `OUTBOUND_IVR_EVIDENCE_MODE=connect`: the handoff code, the DTMF entry, the
> lookup Lambda, the queue, the whisper flow. **The deployed Runtime currently runs
> `OUTBOUND_IVR_EVIDENCE_MODE=direct`**, where `AwsTeamDialer` takes the
> `_request_handoff_direct` branch instead: it dials the evidence number straight into
> the call as a live colleague, with no code, no DTMF and none of the Connect flows below
> firing. The Escalation stack still deploys them, so they exist and are reachable by
> flipping `EVIDENCE_MODE`; they are just not what happens on a call today. Check the
> Runtime's own environment before trusting this section as a description of live
> behaviour.


1. `AwsTeamDialer.request_handoff` (`outbound_ivr/voice/backend.py`) first writes a fresh
   6-digit handoff code into `OUTBOUND_IVR_HANDOFF_TABLE` (`outbound-ivr-handoff-codes`,
   `code` as the key, `case_id`/`items_collected`/`escalation_reason`/`summary`, TTL'd,
   retried on the unlikely collision with a still-live code via a conditional
   `PutItem`), then calls `CreateSIPParticipant` (the same `livekit-api` call
   `livekit_calls.dial_office` already uses for the outbound office leg, reused here
   through `_dial_evidence_team`) into the *same room* the call is already in, pointed
   at `OUTBOUND_IVR_EVIDENCE_NUMBER` through the same outbound trunk
   (`outbound-ivr-chime-outbound`) the office leg already dialed out on: the office leg
   stays exactly where it is.
2. Once that SIP participant answers, Outbound IVR sends the handoff code as DTMF followed by
   `#` over its own room connection (`press_key`, the same `LiveKitTransport.press_key`
   the navigate phase already uses: DTMF is room-scoped, not call-scoped, so it has
   to go out this way rather than through a server-side API call). The Connect handoff
   flow (`outbound-ivr-evidence-handoff`, a `GetParticipantInput` block reading the
   6-digit code) looks it up via the `outbound-ivr-escalation-lookup` Lambda
   (`src/lambdas/escalation_lookup/handler.py`), copies the result into
   contact attributes, and transfers to the Evidence team queue; the agent whisper flow
   (`outbound-ivr-evidence-whisper`) reads the case id, escalation reason, and summary
   back to whichever agent the queue connects, with Polly, before that agent is bridged
   in. The customer-queue hold flow (`outbound-ivr-evidence-queue-hold`) plays a hold
   loop and, if nobody answers in time, a callback message; no real automated
   callback is placed.
3. `AwsTeamDialer` then waits for a `"team-"` participant to join the room
   (`join_waiter`, `LiveKitTransport.wait_for_team_join`): Connect bridges its
   agent into the same SIP leg that answered, so "the SIP leg answered" and "a `team-`
   participant joined" are one event here, not two. The "team-" identity on this leg is
   the SIP participant's own `participant_identity`
   (`livekit_calls.team_identity(room)`), set when `CreateSIPParticipantRequest` is
   issued, so `wait_for_team_join`'s own far-end-exclusion guard applies here too, with no
   separate check for "is this a phone leg or a browser leg."
4. If `OUTBOUND_IVR_EVIDENCE_NUMBER` is not set, `AwsTeamDialer` logs and returns
   `joined=False` at once rather than attempting a dial with nothing to dial: the
   deployed Runtime always has it set (from the escalation nested stack's claimed DID,
   if one was claimed; see `infrastructure/stacks/outbound_ivr_stack.py`'s wiring), but a
   local run with `OUTBOUND_IVR_TEAM_DIALER=aws` and no number configured fails this way
   instead of raising.

Everything above is exercised by `tests/outbound_ivr/test_aws_team_dialer.py` against a
fake LiveKit admin client and a fake handoff table, and by
`tests/infrastructure/test_escalation_stack.py` against the synthesized Connect flows and
Lambda integration.

## 3. How the tool waits

**Decision: the tool waits, up to 30 seconds, for a `"team-"` participant to join, and
returns what Outbound IVR should say next.** It does not return at once and deliver the join as
a separate input later.

**Why this fits Strands `BidiAgent` and Nova Sonic tool timing, and the alternative does
not:** every tool call in this codebase (`record_item`, `escalate`, `complete`, and
`press_key`/`menu_status` in the navigate phase) is a single request/response exchange
inside one Nova Sonic turn: the model calls the tool, `BidiAgent` awaits the coroutine,
and the string that comes back is what the model reasons from next. There is no second
channel today for telling a live Nova Sonic session "something happened" outside of a
tool's own return value or the audio `InputStream` (which only ever carries far-end PCM
or silence, per `CallInputStream.__call__`). Building a "deliver the join as a new
input" path would mean inventing that channel, most plausibly by sending the model a text
turn via `agent.send(...)`, the same primitive `CallInputStream.start` uses for a
seeded opening line, but outside of this codebase's established pattern of "a tool's
return value is the only way information reaches the model mid-turn." Waiting inside the
tool needs nothing new: `transfer_to_team` is `async`, like every other tool here, and the
agent's output pacer and input reader keep running in their own tasks while a tool
coroutine is awaited (that's exactly what already happens for the sub-second
`state.backend.*` calls `escalate`/`complete` make: this is the same mechanism, just a
longer wait). Two things make the 30-second wait safe where a shorter one is implicit
elsewhere:

- **Audio starvation.** `CallInputStream.__call__` already times out after
  `SILENCE_AFTER_SECONDS` (1.0s) and substitutes a silence frame rather than blocking
  forever, specifically because "Bedrock ends a Nova Sonic session after 55 seconds
  without audio." That loop runs independently of the pending tool call, so silence
  keeps flowing to Nova Sonic for the full 30 seconds without the session timing out,
  exactly as it already does during ordinary pauses in conversation.
- **No audio output while waiting is correct, not a gap to paper over.** Outbound IVR already
  said its one line ("Let me bring in a colleague...") before calling the tool; staying
  quiet while that colleague is paged is the realistic behavior, not dead air to mask.

## 4. What happens on join

`transfer_to_team` returns a result that tells the model exactly what to do next (same
pattern as `escalate`/`complete` returning "Thank them... stop talking"), specifically:

1. **Introduce the colleague out loud, warmly, with a one-line summary**: the `summary`
   argument spliced into a fixed carrier sentence and said almost word for word, not
   freely paraphrased (asking Nova Sonic to paraphrase a summary after the tool returned
   is unreliable live: it can say only the fixed pre-tool line and nothing else). E.g.
   "Thanks for picking this up. This is the lumbar MRI prior auth for Jordan Ellis, I
   still need the physical therapy dates confirmed." This is a **warm introduction,
   spoken on the open line both the office and the colleague can hear.**
   There is no hidden whisper to the colleague the way the AWS Connect whisper flow in
   part 2 gives an agent a private briefing before the customer joins. In LiveKit mode,
   every participant in the room hears everything published to it; a "private" aside
   would need a side channel this app doesn't have, and more importantly isn't what a
   real warm handoff sounds like to the person already on the call, who should hear who
   is joining and why.
2. **Outbound IVR says it will leave them to it**: a short, fixed closing line, same "say
   goodbye, then stop talking" instruction shape `escalate`/`complete` already give the
   model, except the content is a handoff line, not a goodbye: something like "I'll let
   you two take it from here" rather than a goodbye proper, since the call keeps going
   without Outbound IVR, not ending.
3. **Outbound IVR ends its own Nova Sonic session**: the current intake phase's `BidiAgent.run()`
   stops, the same way `watch_closing`/`finish_phase` already end a phase today, but
   without the goodbye-then-hangup sequence `begin_closing` drives for `escalate`/`complete`.
   This needs a new terminal mode on `CallState`, e.g. `state.finish_phase("handed_off")`
   as the `next_phase` sentinel (today's sentinels are `"intake"`, after navigate, and
   `None`, meaning "end the call"): `handle_call`'s phase loop (`outbound_ivr/voice/agent.py`)
   must treat `"handed_off"` as "stop running phases, but do not hang up and do not close
   the transport", a third outcome alongside "run another phase" and "end the call",
   where today there are only two.
4. **Outbound IVR's transport stops publishing audio but stays in the room.** `LiveKitTransport`
   needs an explicit "go silent" operation distinct from `hang_up()`: stop feeding
   `send_audio` (or have the agent process simply stop calling it, since nothing is
   driving a `BidiAgent` anymore once its session has ended) while leaving the room
   connection, the SIP leg, and the `CallRecorder` exactly as they are. **Outbound IVR does not
   hang up the SIP leg**, removing the far-end SIP participant is exactly what
   `LiveKitTransport.hang_up()` does today, and that must not run on this path.
5. **The call ends when the far end or the human leaves**, not when Outbound IVR decides it's
   over, because Outbound IVR is no longer the one deciding. `LiveKitTransport._end_call()` is
   already wired to `participant_disconnected` for whichever participant it is tracking as
   the far end, and to the room's own `disconnected` event; the same handler, once
   extended to also watch the `"team-"` participant's `participant_disconnected` event,
   covers "either leg leaves" without new plumbing. Once `_end_call()` fires,
   `frames()` ends (its `None` sentinel), `read_audio` in `agent.py` sees that and calls
   `state.hangup.set()` the way it already does for a real hangup today, except
   `handle_call`'s `finally` block must track that this call ended via handoff (a
   `state.handed_off` flag, set in step 3 above) and skip the `transport.hang_up()` call
   it would otherwise make, since there is nothing left to hang up: the far end already
   left, or the team already left, and the room naturally has no one left to remove.
6. **Then the room is deleted, the recording is closed, and "ended" is written with
   reason `"handed off"`.** `write_case_record`/`log.write("ended", reason=...)` in
   `agent.py` get a third reason string, alongside today's `"far end hung up"` and
   `"agent hung up"`, selected when `state.handed_off` is set. Room deletion is
   `outbound_ivr.voice.livekit_calls`'s existing `delete_room`/`_run_call` teardown path, no
   change needed there, since `_run_call`'s `finally` already deletes the room and stops
   the agent process once it exits, whatever reason it exited for.

   The Runtime's overall call ceiling (`agentcore/livekit_app.py`, `_wait_for_call`) is
   `livekit_calls.CALL_TIMEOUT_S` (420 seconds) until a colleague joins. Once
   `transfer_to_team` sets the call's `handed_off` event it switches to
   `livekit_calls.HANDOFF_MAX_S` from that moment (`OUTBOUND_IVR_HANDOFF_MAX_S`, default
   1800 seconds), so the shorter pre-handoff ceiling does not cut off the office and the
   colleague mid-conversation. The silence backstop above still ends a dead call first.

## 5. No answer in 30 seconds

If no `"team-"` participant joins within the wait in part 3, `TeamDialer.request_handoff`
returns a result meaning "no one answered" rather than raising, a dialer timing out is
an expected outcome here, not a failure of the call itself, the same way `gate_keypress`
returning `(False, reason)` in `navigate.py` is a normal "not yet" rather than an
exception. `transfer_to_team`'s own return string then tells the model:

> No one was available. Apologize briefly, then say the review team will follow up
> instead.

and the tool itself calls `state.backend.escalate(...)` on the model's behalf is **not**
how this works, `escalate` stays a tool the model calls itself, same as always, so the
model's own apology and `escalate` call happen in its next turn, in that order, exactly
the pattern `complete`'s "items still missing" branch already uses (the tool's return
value changes the model's next move; the tool does not silently redirect to a different
business outcome underneath it). The business-rule record of the earlier
`transfer_to_team` call (part 1, status `"handoff_requested"`) is then superseded by
`escalate`'s own final outcome, the same "last final outcome wins" rule `set_outcome`
already enforces for every other tool.

## 6. New events, and the case outcome

Four new event types, written the same way every existing event is
(`state.log.write(type_, **fields)` in `outbound_ivr/voice/agent.py`), consumed by the dashboard
exactly like `"escalate"`/`"handoff"`/`"tool"` already are:

| Event | When | Fields |
|---|---|---|
| `handoff_requested` | `transfer_to_team` is called, before dialing | `reason`, `summary` |
| `handoff_ringing` | the dialer has asked for a human (dashboard notified, or `CreateSIPParticipant` issued) | none |
| `handoff_joined` | a `"team-"` participant joined the room | `identity`, time implicit in the event's own `ts` |
| `handoff_failed` | the 30s wait elapsed with no join | `reason: "no answer"` |

The case outcome gets a fifth value, `"handed off"`, alongside today's `"complete"` and
`"escalated"` (`outbound_ivr.tools.business`'s `store.set_outcome(..., status=...)` calls):

```python
outcome = {
    "type": "handed_off",
    "reason": reason,
    "summary": summary,
    "joined_identity": identity,
    "joined_at": "<iso timestamp>",
}
```

set once `handoff_joined` fires, not at `transfer_to_team`'s own call time: the
`"handoff_requested"` status from part 1 is provisional until a human actually joins (or
isn't, which goes to `escalate` instead, per part 5).

## 7. Recording through the handoff

`CallRecorder` (`outbound_ivr/voice/call_recorder.py`) already mixes two buffers, `_far_buf`
(far end) and `_near_buf` (today, only ever Outbound IVR's own outgoing audio, fed from
`LiveKitTransport.send_audio`). After a handoff, Outbound IVR stops calling `send_audio` (part 4,
step 4), so `_near_buf` would simply go silent unless something else feeds it, but the
call's second half (the office and the team member talking directly) is exactly what the
recording still needs to capture. **The near leg's source switches from Outbound IVR's own
outgoing audio to the `"team-"` participant's audio track once that participant joins**:
`LiveKitTransport` subscribes to it the same way it already subscribes to the far-end
track in `_start_audio_reader`/`_pump_audio`, and feeds each chunk to
`self._recorder.add_agent(payload)`, reusing the existing `near`/`agent` buffer and its
20ms mixing clock rather than adding a third buffer, since only one of "Outbound IVR" or "the
team member" is ever the near-side speaker at a given point in the call. The far side
(the office's SIP participant) is untouched by any of this: `_pump_audio`'s existing feed
into `add_far_end` keeps running exactly as it does today, whichever leg the near side
currently is. This is what "the agent's transport keeps mixing far end + team audio"
means concretely: the mixing clock, the WAV file, and the `far`/`near` split in
`call_recorder.py` are unchanged; only which track feeds the near side changes, and
`LiveKitTransport` is where that switch happens, at the moment `handoff_joined` fires.
