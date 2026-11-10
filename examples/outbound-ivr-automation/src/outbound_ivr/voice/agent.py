"""The IVR Wiz voice agent: LiveKit call audio <-> Strands `BidiAgent` on Bedrock Nova 2.5 Sonic.

Two phases: `navigate` presses DTMF keys through the provider's phone menu, `intake`
talks to the human who answers and collects three pieces of missing prior-authorization
documentation. Each phase gets its own `BidiAgent` (and so its own Nova Sonic session),
because Nova Sonic fixes its instructions at session start — a phase change is a brand
new agent, not an in-place prompt swap.

Audio bridging: the call carries 8 kHz 16-bit mono PCM both ways (see
`outbound_ivr.voice.frames`). Nova Sonic accepts 8 kHz input natively, so caller audio is
forwarded unchanged. Its output is configured for 16 kHz (the smallest ratio down to
8 kHz) and resampled with `soxr` before being framed back onto the call.

`handle_call` is the one entrypoint every call funnels into; the deployed AgentCore
Runtime calls it directly (`agentcore/livekit_app.py` -> `handle_call(LiveKitTransport())`).

Every call this agent places is against synthetic demo data. Pointing it at real
patient information makes that data PHI, which is the deployer's own responsibility: a
HIPAA-eligible service configuration, a Business Associate Agreement with AWS, and your
own compliance review, none of which this module provides. `OUTBOUND_IVR_TRACE_CALL_CONTENT`
(see `infrastructure/stacks/agent_runtime_stack.py`) controls whether call content
reaches traces and logs. It defaults to off here; the deployed path still sets it
explicitly (on by default there, via `-c traceCallContent`).
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import contextlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import numpy as np
import soxr
import structlog
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

from strands import tool
from strands.bidi.agent import BidiAgent
from strands.bidi.models import BedrockNovaSonicModel
from strands.bidi.types.events import (
    BidiAudioDeltaEvent,
    BidiBargeInEvent,
    BidiOutputEvent,
    BidiResponseStartEvent,
    BidiResponseStopEvent,
    BidiTranscriptBlockEvent,
)
from strands.bidi.types.io import InputStream, OutputStream
from strands.bidi.types.media import AudioDelta
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from outbound_ivr import intake, navigate, prompts
from outbound_ivr.tools import rendering
from outbound_ivr.tools.store import LocalArtifactStore, S3ArtifactStore
from outbound_ivr.voice import frames
from outbound_ivr.voice.backend import (
    AwsTeamDialer,
    CaseNotFoundError,
    IntakeBackend,
    TeamDialer,
    make_backend,
)
from outbound_ivr.voice.transport import Transport, TransportError

logger = structlog.get_logger("outbound_ivr.voice.agent")

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "runs"
LOGS = ROOT / "logs"

MODEL_ID = os.environ.get("OUTBOUND_IVR_SONIC_MODEL", "amazon.nova-2-5-sonic")
VOICE = os.environ.get("OUTBOUND_IVR_VOICE", "tiffany")
# AgentCore Runtime (and Lambda) always set AWS_REGION to the deployment region.
REGION = os.environ["AWS_REGION"]

INPUT_SAMPLE_RATE = 8000  # The call's native rate; Nova Sonic also takes 8 kHz natively.
OUTPUT_SAMPLE_RATE = 16000  # Nova Sonic output, resampled down to 8 kHz for the call.

NAVIGATE_GOAL = (
    "Reach a person at the provider's office who can help with a prior "
    "authorization request for an MRI, which is an imaging study. Choose provider or "
    "insurance options over patient options."
)

DTMF_DIGITS = set("0123456789*#")
HEARD_WINDOW = 4

# How soon Nova decides the person has finished speaking, in the conversation phase:
# LOW waits longest, HIGH ends the turn soonest (faster replies, more risk of cutting in on
# a pause). Measured on 2026-10-13: with MEDIUM, turn detection was about 2.5 s of the
# 4.2 s between the front desk stopping and Outbound IVR starting; HIGH brought the average from 3.9 s to
# 3.45 s with every completed call still passing. The menu phase stays on LOW.
INTAKE_ENDPOINTING = os.environ.get("OUTBOUND_IVR_INTAKE_ENDPOINTING", "HIGH").upper()

# A transcript that is only a parenthetical or a bracketed note, e.g. "(silence)" or
# "[Silence]": a stage direction, not speech.
STAGE_DIRECTION = re.compile(r"\(\s*[^()]*\s*\)|\[\s*[^\[\]]*\s*\]")

# Nova sometimes narrates its own reasoning instead of actually staying silent, wrapped in
# a tag it invented, e.g. "<silent> Thinking Process: ...</silent>" or a bare
# "<silent>\n</silent>". It is heard aloud, not just logged (the audio for that turn is
# already queued by the time a transcript block arrives), so strip it before logging --
# whatever real speech is left over, if any, still counts as "said".
TAG_DIRECTION = re.compile(r"<([a-zA-Z][\w-]*)\b[^<>]*>.*?</\1\s*>", re.DOTALL)

# A tag with no matching close, e.g. a bare "<silent>" on its own: TAG_DIRECTION above
# needs a closing tag to strip a pair, so this catches the leftover opening or closing
# half by itself.
BARE_TAG = re.compile(r"</?[a-zA-Z][\w-]*\s*/?>")

# Nova sometimes writes its chain-of-thought instead of staying silent, e.g.
# "Thinking Process: I should wait quietly here." A whole transcript starting this way
# is reasoning, not something said to the other party.
THINKING_PREFIX = re.compile(r"(?is)^\s*thinking process\s*:")


def _strip_tag_narration(text: str) -> str:
    return TAG_DIRECTION.sub("", text).strip()


def _is_stage_direction(text: str) -> bool:
    """True if `text` (a transcript's full text, already stripped of any <tag>...</tag>
    pair) is only a stage direction -- "(silence)", "[Silence]", a bare invented tag like
    "<silent>", or Nova narrating its own reasoning -- rather than something actually
    said to the other party. Shared by `CallState.say` (what gets logged/kept in the
    transcript) and `CallOutputStream` (whether that response's audio gets played
    at all, see `__call__` below)."""
    stripped = _strip_tag_narration(text)
    if not stripped:
        return True
    return bool(
        STAGE_DIRECTION.fullmatch(stripped)
        or BARE_TAG.fullmatch(stripped)
        or THINKING_PREFIX.match(stripped)
    )


# After a transfer the line may ring or sit silent before a person picks up, so the
# intake phase of a full call waits for them instead of speaking into the void. The
# fixed opening (with its disclosure) moves into the system prompt for that case.
TRANSFER_NOTE = """

YOU HAVE JUST BEEN TRANSFERRED
The phone menu has put you through. For a while you may still hear ringing, silence,
hold music, or recorded messages such as "please hold", "transferring your call", "your
call is very important to us", "calls are answered in the order received", or "all of
our representatives are busy". Those are not a person: do not answer them, not even to
say you will wait. Say nothing until a live person greets you, for example with hello or
the name of the office. When they do, your first words must be exactly this, word for word:
"{opening}"
"""

# If the call sends nothing for this long (silence suppression, a ringing transfer, a
# hold, or `transfer_to_team`'s own wait for a colleague), feed the model silence
# instead: Bedrock ends a Nova Sonic session after 55 seconds without audio, and
# (found live, 2026-10-17, three "Timed out waiting for audio bytes" ValidationExceptions
# during exactly these waits) Nova Sonic also expects audio at roughly a real-time
# cadence, not just "something within 55 seconds" -- one 100 ms silence frame every
# full second is ten times slower than real time and let the gap accumulate until Nova
# gave up. This now matches SILENCE_FRAME's own duration (100 ms), so a quiet stretch of
# any length is fed a steady, real-time-paced trickle of silence.
SILENCE_AFTER_SECONDS = 0.1
SILENCE_FRAME = bytes(1600)  # 100 ms of 8 kHz 16-bit silence, matching SILENCE_AFTER_SECONDS

# Live voice levels for the local dashboard's speaking indicators: runs/levels/<call>.jsonl,
# ten lines a second. Off with OUTBOUND_IVR_LEVELS=0 (in AWS there is no dashboard to feed).
LEVELS_ENABLED = os.environ.get("OUTBOUND_IVR_LEVELS", "1") not in ("", "0", "false", "no")
LEVELS_SECONDS = 0.1

# Nova Sonic produces audio faster than real time. Sent in a burst, a telephony leg can
# drop audio and cut words out of sentences, so the output is paced: one 20 ms frame
# every 20 ms.
FRAME_SECONDS = 0.02

# After complete or escalate the agent says goodbye, then hangs up: once its audio has
# finished playing and nothing new has come for this long (which also covers the gap
# before the model answers the tool result), or after CLOSING_MAX_SECONDS.
CLOSING_QUIET_SECONDS = 2.5
CLOSING_MAX_SECONDS = 20.0

# A phase's Nova Sonic session can fail with a transient error (ModelStreamErrorException,
# throttling, a dropped connection) rather than anything the call did. Reopen the phase
# with a fresh BidiAgent session -- same transport, same CallState -- up to this many
# times before ending the call the way a non-retryable error always has.
PHASE_RETRIES = int(os.environ.get("OUTBOUND_IVR_PHASE_RETRIES", "2"))

# Exception class names (and botocore "Error"."Code" values) worth retrying a phase for.
RETRYABLE_ERROR_NAMES = {
    "ModelStreamErrorException",
    "ThrottlingException",
    "ServiceUnavailableException",
    "ServiceUnavailable",
    "ModelTimeoutException",
    "InternalServerException",
}

# If navigate hears nothing at all for this long, the model or the audio bridge is
# stuck, not just a caller slowly working through a menu: restart the phase instead of
# waiting out Bedrock's own eventual error (observed: 3 minutes of silence before a
# ModelStreamErrorException -- see run_navigate_phase for the reasoning).
NAVIGATE_WATCHDOG_SECONDS = float(os.environ.get("OUTBOUND_IVR_NAVIGATE_WATCHDOG_SECONDS", "45"))

# Nova Sonic has no sense of elapsed time on its own: if the office side goes silent on a
# hold and never says another word (never offers a second hold or transfer the model
# could count), the "two exchanges in a row" / "more than twice in a row" stall rules in
# prompts.PRIOR_AUTH_PROMPT and prompts.TRANSFER_TO_TEAM_NOTE can never fire, because
# nothing ever happens for the model to notice. During the intake phase only, if nothing
# new has been heard for this long, send the model a text turn saying so, so it can apply
# that same stall judgement to a hold that is simply taking too long, not just to a
# second hold or transfer offer.
INTAKE_HOLD_NUDGE_SECONDS = float(os.environ.get("OUTBOUND_IVR_INTAKE_HOLD_NUDGE_SECONDS", "45"))

INTAKE_HOLD_NUDGE_TEXT = (
    "A long silence has passed since the last thing you heard, with nobody new coming "
    "on the line yet. Decide now, using your own instructions, whether this still "
    "counts as them cooperating or whether it has gone on long enough to count as the "
    "kind of stall you are told to act on."
)

# Credential resolution can shell out to a slow `credential_process`. A single
# boto3.Session, reused for every phase and every call, means that cost is paid once per
# process instead of once per Nova Sonic connection.
_BOTO_SESSION = boto3.Session(region_name=REGION)


def _ts() -> str:
    """Millisecond-precision UTC timestamp, matching the contract's event log format."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# Event fields that carry what was actually said on the call: the spoken lines,
# the menu option quoted back from the far end's audio, a recorded answer, and
# the model's own free-text summary/reason. Everything else on an event (digits,
# status labels, keys, outcomes) is structure, not content.
_CONTENT_FIELDS = frozenset({"text", "option", "value", "summary", "reason"})


# Fields that carry the case itself rather than a line of conversation: the
# "ready" event's case summary and checklist, which hold the patient's name, date
# of birth and member ID. They are not in _CONTENT_FIELDS because they are
# structures, not strings, and they are excluded from the logger.info line
# entirely, so only the stdout record has to deal with them.
_CASE_FIELDS = frozenset({"case", "checklist"})


def _loggable_record(record: dict[str, Any]) -> dict[str, Any]:
    """A whole event record, safe to print to a log, unless content tracing is on.

    Same switch as `_loggable`, applied to the full record rather than just the
    event's own fields, plus the nested case payloads `_loggable` never sees.
    """
    if _trace_call_content():
        return record
    redacted = _loggable(record)
    for key in _CASE_FIELDS & redacted.keys():
        value = redacted[key]
        redacted[key] = f"<redacted {len(value)} {'keys' if isinstance(value, dict) else 'items'}>"
    return redacted


def _trace_call_content() -> bool:
    return os.environ.get("OUTBOUND_IVR_TRACE_CALL_CONTENT", "0").lower() not in (
        "0",
        "false",
        "no",
    )


def _loggable(fields: dict[str, Any]) -> dict[str, Any]:
    """`fields` with call content replaced by its length, unless content tracing
    is on.

    The structured record still carries everything: it goes to the events table
    (customer-managed key, TTL'd) and to the dashboard, which is the product. This
    only governs the human-readable line that lands in the container's stdout and
    from there in CloudWatch Logs, which has neither of those protections and used
    to print every spoken word regardless of OUTBOUND_IVR_TRACE_CALL_CONTENT. Keeping
    the length makes turn-taking debuggable without the words.
    """
    if _trace_call_content():
        return fields
    return _redact_content(fields)


def _redact_content(fields: dict[str, Any]) -> dict[str, Any]:
    """`fields` with every _CONTENT_FIELDS value replaced by its length, at any depth.

    Nested dicts matter: a `tool` event's `input` is the tool call's own arguments, so
    `record_item`'s recorded answer arrives as `input["value"]` and `press_key`'s quoted
    menu option as `input["option"]`, never as top-level fields.
    """
    return {
        key: (
            f"<redacted {len(str(value))} chars>"
            if key in _CONTENT_FIELDS and value
            else _redact_content(value)
            if isinstance(value, dict)
            else value
        )
        for key, value in fields.items()
    }


def _content_for_log(text: str) -> str:
    """A line of call text for a log message: itself only if content tracing is on."""
    return text if _trace_call_content() else f"<redacted {len(text)} chars>"


class EventLog:
    """Appends one JSON object per line, and optionally forwards it elsewhere.

    `sink="file"` (the deployed Runtime's value today, see `handle_call`) appends to
    runs/events/<call_uuid>.jsonl. `sink="stdout"` prints the same JSON line to stdout
    instead, for CloudWatch Logs to pick up. `forward`, when given, is called with the
    full record for every event -- `handle_call` uses it to also forward events to
    DynamoDB (`OUTBOUND_IVR_EVENTS_TABLE`) for the dashboard API to read.
    """

    def __init__(
        self,
        call_uuid: str,
        *,
        sink: str = "file",
        forward: Any = None,
    ) -> None:
        self.call_uuid = call_uuid
        self.sink = sink
        self._forward = forward
        self._fh = None
        self.path = None
        if sink == "file":
            events_dir = RUNS / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            self.path = events_dir / f"{call_uuid}.jsonl"
            self._fh = self.path.open("a", encoding="utf-8")
        elif sink != "stdout":
            raise ValueError(f"EventLog sink must be 'file' or 'stdout', not {sink!r}")

    def write(self, type_: str, **fields: Any) -> None:
        record = {"ts": _ts(), "call": self.call_uuid, "type": type_, **fields}
        if self._fh is not None:
            # A local file under events/, read by the local dashboard. It is the
            # event store for that mode, not a log, so it keeps the full record
            # for the same reason `self._forward` (DynamoDB) does.
            self._fh.write(json.dumps(record) + "\n")
            self._fh.flush()
        else:
            # stdout, which on the AgentCore Runtime IS the log: it goes straight
            # to CloudWatch Logs. This used to print the raw record, so the
            # patient's name, date of birth and member ID went to CloudWatch on
            # every event no matter what OUTBOUND_IVR_TRACE_CALL_CONTENT said. Only the
            # logger.info line below was ever redacted, and `caplog` in the tests
            # could not see this one, which is how it went unnoticed.
            print(json.dumps(_loggable_record(record)), flush=True)
        if type_ not in ("ready",):  # ready carries the whole case; the dashboard shows it
            detail = " ".join(f"{k}={v}" for k, v in _loggable(fields).items())
            logger.info("call_event", call_id=self.call_uuid[:8], event_type=type_, detail=detail)
        if self._forward is not None:
            self._forward(record)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


# Matches lambdas/api/README.md's "now + 7 days" for the outbound-ivr-events table's own TTL
# attribute.
_EVENTS_TTL_SECONDS = 7 * 24 * 3600


class _DynamoDBEventSink:
    """Writes every `EventLog` record as one `OUTBOUND_IVR_EVENTS_TABLE` item, in exactly the
    shape `lambdas/api/handler.py`'s `GET /api/calls/{call}/events` already reads:
    `call` (partition key, already on every record), `seq` (sort key -- a per-call
    counter kept here, since this process is the only writer for its own call, no
    coordination needed), `t` (renamed from the record's own `ts` -- the table's
    documented field name, see lambdas/api/README.md), `type` plus whatever other
    fields that event type carries, copied verbatim, and `expires_at` (the table's TTL
    attribute, now + 7 days).

    Used as one of `EventLog`'s `forward` callables (see `handle_call`), gated on
    `OUTBOUND_IVR_EVENTS_TABLE` being set, so a run without that table never constructs one of
    these at all.
    """

    def __init__(self, table_name: str) -> None:
        # Read fresh here, like S3ArtifactStore's own os.environ["AWS_REGION"] -- not
        # the module-level REGION constant, which is captured once at this module's
        # own import time and would miss AWS_REGION being set afterward.
        region = os.environ["AWS_REGION"]
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self._seq = 0

    def write(self, record: dict[str, Any]) -> None:
        item = {k: v for k, v in record.items() if k != "ts"}
        item["t"] = record.get("ts")
        item["seq"] = self._seq
        item["expires_at"] = int(time.time()) + _EVENTS_TTL_SECONDS
        self._seq += 1
        try:
            self._table.put_item(Item=item)
        except Exception:
            logger.warning(
                "dynamodb_event_write_failed",
                call_id=record.get("call"),
                event_type=record.get("type"),
                exc_info=True,
            )


def _combine_forwards(*forwards: Any) -> Any:
    """One `EventLog`-style `forward` callable that calls each of `forwards` in turn --
    e.g. the transport's own `send_event` plus a `_DynamoDBEventSink`, when both apply
    to this call."""
    forwards = [f for f in forwards if f is not None]
    if not forwards:
        return None
    if len(forwards) == 1:
        return forwards[0]

    def _forward(record: dict[str, Any]) -> None:
        for fwd in forwards:
            fwd(record)

    return _forward


def _level(pcm: bytes) -> float:
    """Loudness of 16-bit PCM on a 0..1 scale: -50 dBFS and below is 0, full scale is 1."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(samples * samples))) / 32768.0
    db = 20.0 * np.log10(rms + 1e-9)
    return round(min(1.0, max(0.0, (db + 50.0) / 50.0)), 3)


class LevelMeter:
    """Peak voice level of each side of the call, written ten times a second.

    `far` is what the call sends us (the menu, then the person); `agent` is Outbound IVR's audio
    as it goes out, after pacing, so the dashboard moves in time with what the caller hears.
    """

    def __init__(self, call_uuid: str) -> None:
        levels_dir = RUNS / "levels"
        levels_dir.mkdir(parents=True, exist_ok=True)
        self._fh = (levels_dir / f"{call_uuid}.jsonl").open("a", encoding="utf-8")
        self._far = 0.0
        self._agent = 0.0
        self._task: asyncio.Task | None = None

    def feed_far(self, pcm: bytes) -> None:
        self._far = max(self._far, _level(pcm))

    def feed_agent(self, pcm: bytes) -> None:
        self._agent = max(self._agent, _level(pcm))

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(LEVELS_SECONDS)
            line = {"t": round(time.time(), 2), "far": self._far, "agent": self._agent}
            self._far = self._agent = 0.0
            self._fh.write(json.dumps(line) + "\n")
            self._fh.flush()

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._fh.close()


class _PhaseEnd(Exception):
    """Raised from the input stream to unwind a phase cleanly: hangup or a tool ended it."""


@dataclass
class CallState:
    """Shared between both phases of one call: the record being built, and what's been heard."""

    call_uuid: str
    mode: str
    log: EventLog
    transport: Transport | None = None
    backend: IntakeBackend | None = None
    case_id: str | None = None
    # Resolved once in handle_call: the case to render the intake prompt from
    # (outbound_ivr.tools.rendering), or None to use the fixed demo prompts.CASE.
    case: dict | None = None
    record: dict = field(default_factory=intake.new_record)
    heard: list[str] = field(default_factory=list)
    raw_audio: asyncio.Queue = field(default_factory=asyncio.Queue)
    hangup: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    next_phase: str | None = None
    # Set by complete/escalate: the call is wrapping up and the agent hangs up after its
    # goodbye. `closing_since` is the loop time it started.
    closing_since: float | None = None
    levels: LevelMeter | None = None
    # Digits pressed so far on this call (across retries), for the navigate resume note.
    pressed_keys: list[dict] = field(default_factory=list)
    # How much had been heard when the last key was pressed: "completed" is judged only on
    # what the far end said after that press.
    heard_at_press: int = 0
    # Set by `_navigate_watchdog` and consumed by `run_navigate_phase`: a `finish_phase`
    # that means "restart this phase", not "it's actually over".
    watchdog_triggered: bool = False
    # LiveKit mode only (docs/escalation-handoff.md): how `transfer_to_team` reaches a
    # human. `None` on every other transport, and on LiveKit calls that never hand off.
    team_dialer: TeamDialer | None = None
    # Set by `transfer_to_team` once a colleague has joined: `handle_call`'s `finally`
    # must not hang up the SIP leg on this path, and the "ended" event's reason is
    # "handed off" rather than "far end hung up" / "agent hung up".
    handed_off: bool = False
    # `time.monotonic()` when the last audio frame arrived from the far end, set by
    # `read_audio` on every frame. After a handoff this is the only honest signal that
    # the call is still alive: see `_wait_for_handoff_to_end`.
    last_far_audio: float = field(default_factory=time.monotonic)
    # What `begin_closing` should hand `finish_phase` once the goodbye (or, on a
    # handoff, the warm introduction) has finished playing: `None` ends the call, as it
    # always has; `"handed_off"` instead leaves the room's audio/SIP leg alone.
    closing_next_phase: str | None = None
    # Set the first time `transfer_to_team` actually starts a handoff this call (not on
    # a validation failure): one handoff per call, see `make_transfer_to_team_tool`.
    transfer_to_team_requested: bool = False
    # Set once `transfer_to_team` has been tried and nobody answered (docs/
    # escalation-handoff.md): `escalate`, in LiveKit mode, is refused until this is true,
    # unless there is no `team_dialer` to try it with in the first place (see `escalate`).
    transfer_to_team_failed: bool = False

    def hear(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.heard.append(text)
        self.record["transcript"].append({"who": "far_end", "text": text, "t": time.time()})
        self.log.write("heard", text=text)

    def say(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        stripped = _strip_tag_narration(text)
        if stripped != text:
            logger.info(
                "stripped_tag_narration_not_logged_as_said",
                call_id=self.call_uuid[:8],
                text=_content_for_log(text),
            )
            text = stripped
        if not text or _is_stage_direction(text):
            # Nova writes these while it waits on hold ("(silence)", "[Silence]",
            # "Thinking Process: ..."). They aren't speech, so they stay out of the
            # transcript and the evidence.
            logger.info(
                "stage_direction_not_logged_as_said",
                call_id=self.call_uuid[:8],
                text=_content_for_log(text),
            )
            return
        self.record["transcript"].append({"who": "agent", "text": text, "t": time.time()})
        self.log.write("said", text=text)

    @property
    def recent_heard(self) -> str | None:
        tail = self.heard[-HEARD_WINDOW:]
        return " ".join(tail) if tail else None

    @property
    def heard_since_press(self) -> str | None:
        tail = self.heard[self.heard_at_press :][-HEARD_WINDOW:]
        return " ".join(tail) if tail else None

    def begin_closing(self, next_phase: str | None = None) -> None:
        """Let the agent finish speaking (a goodbye, or a handoff's warm introduction);
        `watch_closing` then ends the phase once it has, with `next_phase` as the
        `finish_phase` sentinel -- `None` (the default) ends the call as it always has,
        `"handed_off"` instead leaves the room alone (see `CallState.closing_next_phase`
        and `handle_call`)."""
        if self.closing_since is None:
            self.closing_since = asyncio.get_running_loop().time()
            self.closing_next_phase = next_phase

    def finish_phase(self, next_phase: str | None) -> None:
        """Signal the current phase to end. Idempotent: the first call wins."""
        if self.done.is_set():
            return
        self.next_phase = next_phase
        self.done.set()
        self.raw_audio.put_nowait(None)  # wake a blocked input stream


# --------------------------------------------------------------------------
# Audio bridging: custom InputStream/OutputStream over one call's Transport.
# --------------------------------------------------------------------------


class CallInputStream(InputStream):
    """Feeds queued call-audio PCM payloads to the model as audio deltas.

    Reads from `state.raw_audio`, filled by the connection's frame reader. A `None` on the
    queue is the end-of-phase sentinel (hangup, or a tool decided this phase is done): it
    raises `_PhaseEnd`, which is how `BidiAgent.run()` is told the phase is over.
    `CallState.finish_phase` puts exactly one, to wake whichever phase's input stream is
    currently blocked in `get()`; it is consumed here, not put back, so the next phase (if
    any) starts against a queue of real audio only, not a stale sentinel that would make its
    own input stream think it had already ended before sending it a single frame.
    """

    def __init__(self, state: CallState, opening: bool = False, seed: str | None = None) -> None:
        self._state = state
        self._opening = opening
        # The seed text that makes the model open with the fixed line (prompts.opening_seed
        # by default, or a case-driven equivalent from outbound_ivr.tools.rendering).
        self._seed = seed if seed is not None else prompts.opening_seed()[0]["content"]

    async def start(self, agent: BidiAgent) -> None:
        if self._opening:
            # Not logged here: the model's own transcript of the line is the "said" event.
            await agent.send(self._seed)

    async def stop(self) -> None:
        pass

    async def __call__(self) -> AudioDelta:
        try:
            payload = await asyncio.wait_for(self._state.raw_audio.get(), SILENCE_AFTER_SECONDS)
        except TimeoutError:
            payload = SILENCE_FRAME
        if payload is None:
            raise _PhaseEnd()
        return AudioDelta(format="pcm", source={"bytes": payload})


class CallOutputStream(OutputStream):
    """Writes the model's audio back onto the call as paced 8 kHz PCM.

    Audio is buffered and a pacer task sends one 20 ms frame every 20 ms (see
    FRAME_SECONDS). Also turns completed transcript blocks into `heard`/`said` events and
    case-record transcript entries, and drops unplayed audio on barge-in.
    """

    def __init__(self, conn: Transport, state: CallState) -> None:
        # `conn` only needs `send_audio`; it's the call's Transport in handle_call, or a
        # bare fake in tests, which construct this directly.
        self._conn = conn
        self._state = state
        self._in_rate = OUTPUT_SAMPLE_RATE
        self._buf = bytearray()
        self._pacer: asyncio.Task | None = None
        self._closing: asyncio.Task | None = None
        self.last_audio_at = 0.0  # loop time the model last produced audio
        self.played_at = 0.0  # loop time the last frame went out to the call
        self._response_audio = 0  # 8 kHz bytes produced by the current model response
        self._response_text: list[str] = []
        # Audio for the current response, held back until its text (so far) says
        # whether this response is only a stage direction ("[Silence]", "<silent>",
        # "Thinking Process: ...") -- see __call__. If text never arrives before the
        # response ends, _response_decided stays False and the pending audio is dropped
        # at BidiResponseStopEvent rather than played.
        self._pending_audio = bytearray()
        self._response_decided = False

    async def start(self, agent: BidiAgent) -> None:
        self._in_rate = agent.model.get_audio_config()["output"]["sample_rate"]  # type: ignore[attr-defined]
        self._pacer = asyncio.create_task(self._pace())
        self._closing = asyncio.create_task(watch_closing(self._state, self))

    @property
    def playing(self) -> bool:
        # Less than one frame left is the unplayable tail of a sentence, not audio still
        # going out: counting it kept a finished goodbye "playing" forever.
        return len(self._buf) >= frames.AUDIO_FRAME_BYTES

    async def _pace(self) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while True:
            if len(self._buf) < frames.AUDIO_FRAME_BYTES:
                # Nothing (or less than a frame) to play: idle, and restart the clock so the
                # next sentence doesn't try to catch up on time spent silent.
                await asyncio.sleep(FRAME_SECONDS)
                next_at = loop.time()
                continue
            frame = bytes(self._buf[: frames.AUDIO_FRAME_BYTES])
            del self._buf[: frames.AUDIO_FRAME_BYTES]
            try:
                await self._conn.send_audio(frame)
            except (ConnectionError, OSError, TransportError):
                return
            self.played_at = loop.time()
            if self._state.levels:
                self._state.levels.feed_agent(frame)
            next_at += FRAME_SECONDS
            await asyncio.sleep(max(0.0, next_at - loop.time()))

    async def drain(self, timeout: float) -> None:
        """Wait (up to `timeout`) for buffered audio to finish playing."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self._buf) >= frames.AUDIO_FRAME_BYTES and loop.time() < deadline:
            await asyncio.sleep(FRAME_SECONDS)

    async def stop(self) -> None:
        if not self._state.hangup.is_set():
            await self.drain(timeout=15.0)
        for task in (self._pacer, self._closing):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._buf.clear()
        self._pending_audio.clear()

    async def __call__(self, event: BidiOutputEvent) -> None:
        if isinstance(event, BidiAudioDeltaEvent):
            pcm = base64.b64decode(event.audio)
            audio = _resample_to_8k(pcm, self._in_rate)
            self._response_audio += len(audio)
            self.last_audio_at = asyncio.get_running_loop().time()
            if self._response_decided:
                # Either this response's text has already shown it's real speech, or
                # the decision came from an earlier call and this is a later response
                # entirely (BidiResponseStartEvent resets the flag below) -- either way,
                # play it as it arrives, same as before this suppression existed.
                self._buf.extend(audio)
            else:
                # Not decided yet: hold the audio rather than play it, in case the text
                # that explains this response turns out to be only a stage direction.
                # If text arrives before (or with) the audio, as it usually does, this
                # buffers at most the gap of one response; if audio arrives first, see
                # BidiResponseStopEvent below for what happens if text never comes.
                self._pending_audio.extend(audio)
        elif isinstance(event, BidiTranscriptBlockEvent):
            if event.role == "assistant":
                self._response_text.append(event.transcript)
                self._state.say(event.transcript)
                if not self._response_decided and not _is_stage_direction(
                    " ".join(self._response_text)
                ):
                    # Real speech, not (only) a stage direction: release whatever audio
                    # was held back for this response, and stop holding back the rest.
                    self._response_decided = True
                    self._buf.extend(self._pending_audio)
                    self._pending_audio.clear()
            else:
                self._state.hear(event.transcript)
        elif isinstance(event, BidiResponseStartEvent):
            self._response_audio, self._response_text = 0, []
            self._pending_audio = bytearray()
            self._response_decided = False
        elif isinstance(event, BidiResponseStopEvent):
            # How much the model actually said aloud, next to what it wrote: shows up text
            # it produced without speaking (e.g. "(silence)" while waiting on hold).
            if self._response_text or self._response_audio:
                logger.info(
                    "response_audio_and_text",
                    call_id=self._state.call_uuid[:8],
                    audio_ms=self._response_audio // 16,
                    text=_content_for_log(" ".join(self._response_text)[:120]),
                )
            if not self._response_decided and self._pending_audio:
                # The response ended without its text ever showing real speech (either
                # no text arrived at all, or everything it said was a stage direction):
                # drop the held-back audio instead of playing it late.
                logger.info(
                    "suppressed_stage_direction_audio",
                    call_id=self._state.call_uuid[:8],
                    bytes_not_played=len(self._pending_audio),
                )
            self._pending_audio = bytearray()
        elif isinstance(event, BidiBargeInEvent):
            self._buf.clear()
            self._pending_audio.clear()


async def watch_closing(state: CallState, output: CallOutputStream) -> None:
    """End the phase once the goodbye (or a handoff's warm introduction) has been said
    and played, after complete/escalate/transfer_to_team.

    The model doesn't necessarily start speaking the moment `begin_closing` is called --
    `transfer_to_team`'s warm introduction in particular is only composed *after* the
    tool call returns, so there can be a real gap, not silence to cut short, before the
    first frame of that reply reaches `output`. The quiet check below only fires once
    the model has produced at least one frame at or after `closing_since`: before that,
    only `CLOSING_MAX_SECONDS` (a generous absolute cap, in case the model never speaks
    at all) can end the phase.
    """
    loop = asyncio.get_running_loop()
    while not state.done.is_set():
        await asyncio.sleep(0.2)
        if state.closing_since is None:
            continue
        now = loop.time()
        spoken = (
            output.last_audio_at >= state.closing_since or output.played_at >= state.closing_since
        )
        last_sound = max(output.last_audio_at, output.played_at, state.closing_since)
        quiet = now - last_sound >= CLOSING_QUIET_SECONDS
        if (
            spoken and quiet and not output.playing
        ) or now - state.closing_since >= CLOSING_MAX_SECONDS:
            state.finish_phase(state.closing_next_phase)


def _resample_to_8k(pcm: bytes, in_rate: int) -> bytes:
    if in_rate == INPUT_SAMPLE_RATE:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2")
    resampled = soxr.resample(samples, in_rate, INPUT_SAMPLE_RATE)
    return np.asarray(resampled, dtype="<i2").tobytes()


# --------------------------------------------------------------------------
# Tools (docs/design.md "Agent behaviour"): press_key/menu_status for
# navigate, record_item/escalate/complete for intake, built per call so each tool closes
# over that call's CallState.
# --------------------------------------------------------------------------


def make_navigate_tools(state: CallState) -> list[Any]:
    @tool
    async def press_key(digit: str, option: str) -> str:
        """Press one DTMF key on the call.

        Only call this after you can quote the menu option you are choosing, copied from
        what the far end just said. A menu path like "2 then 2" needs two separate calls,
        one per digit, even when the digit repeats.

        Args:
            digit: the single key to press: 0-9, '*', or '#'.
            option: the menu option you are choosing, quoted from what you just heard.
        """
        if digit not in DTMF_DIGITS:
            return f"'{digit}' is not a valid DTMF digit (0-9, *, #)."
        ok, reason = navigate.gate_keypress(option, state.recent_heard)
        if not ok:
            state.log.write("status", value="wait")
            return f"Not pressed: {reason}. Wait for the right option before pressing again."
        await state.transport.press_key(digit)
        state.pressed_keys.append({"digit": digit, "option": option})
        state.heard_at_press = len(state.heard)
        state.log.write("dtmf", digit=digit, option=option)
        return "Pressed."

    @tool
    async def menu_status(status: str) -> str:
        """Report the state of the menu.

        Args:
            status: "wait" (no option fits yet, keep listening), "completed" (you heard
                transfer language and a human is coming on the line), or "stuck" (no
                progress after repeated tries).
        """
        if status == "completed":
            ok, reason = navigate.gate_completed(state.heard_since_press)
            if not ok:
                state.log.write("status", value="wait", rejected="completed")
                return (
                    f"Not completed yet: {reason}. If you just pressed a key, wait quietly "
                    "for what comes next; otherwise listen for the option that fits the goal."
                )
        if status == "stuck" and state.heard_at_press and state.heard_since_press is None:
            # Right after a key press the menu is usually silent for a moment; that is
            # not being stuck (the dashboard flashed STUCK on every press). A menu that
            # really goes quiet is caught by the navigate watchdog.
            state.log.write("status", value="wait", rejected="stuck")
            return "Not stuck: you just pressed a key and the menu has not answered yet. Wait quietly for what comes next."
        state.log.write("status", value=status)
        if status == "completed":
            state.finish_phase("intake")
            return "Moving to the intake conversation."
        return "Status noted."

    return [press_key, menu_status]


def make_intake_tools(state: CallState) -> list[Any]:
    @tool
    async def record_item(key: str, value: str, confirmed: bool) -> str:
        """Save one confirmed piece of missing prior-auth documentation.

        Args:
            key: which missing item this answers (one of the case's missing-item keys).
            value: the answer as a short factual statement in the caller's words.
            confirmed: true only if the caller confirmed your read-back.
        """
        if key not in intake.item_keys(state.record):
            return f"'{key}' is not one of this case's missing items."
        await state.backend.record_item(
            case_id=state.case_id, call=state.call_uuid, key=key, value=value, confirmed=confirmed
        )
        state.log.write("got", key=key, value=value, confirmed=bool(confirmed))
        return "Saved."

    @tool
    async def escalate(reason: str, follow_up: str = "") -> str:
        """Stop collecting and hand the call to a human on the payer side.

        This is for a real dead end only: the front desk refuses or cannot give you a
        missing item and offers no workable alternative, or asks to speak with someone
        on your own side. Being asked to hold, offered a transfer to another
        department, or told they'll go find someone to help is cooperation, not a dead
        end -- hold quietly and continue when someone new comes on; do not call this
        tool for that.

        Args:
            reason: why the call is being escalated.
            follow_up: who to follow up with and when, if the caller offered it.
        """
        # docs/escalation-handoff.md, part 1: a dead end tries a colleague first.
        # Refuse until `transfer_to_team` has been tried on this call and failed (no
        # answer); an "impossible" handoff -- no `team_dialer` for this call to try it
        # with at all -- does not block escalate, since there is nothing to try.
        if state.team_dialer is not None and not state.transfer_to_team_failed:
            return "First bring in a colleague with transfer_to_team."
        await state.backend.escalate(
            case_id=state.case_id, call=state.call_uuid, reason=reason, follow_up=follow_up or None
        )
        state.log.write("escalate", reason=reason, follow_up=follow_up)
        state.log.write("handoff", outcome="escalated", reason=reason, follow_up=follow_up)
        state.begin_closing()
        return "Escalated. Thank them in one short sentence, say goodbye, and stop talking."

    @tool
    async def complete(summary: str) -> str:
        """Finish the call after all items are collected or accounted for.

        Args:
            summary: one or two sentences on what the office will send and when.
        """
        outcome = await state.backend.complete(
            case_id=state.case_id, call=state.call_uuid, summary=summary
        )
        if outcome["type"] == "escalated":
            state.log.write(
                "handoff",
                outcome="escalated",
                reason=outcome["reason"],
                missing=outcome.get("missing", []),
            )
            state.begin_closing()
            return (
                "Some items are still missing; this call is being escalated instead. Say the "
                "review team will follow up on them, thank them, say goodbye, and stop talking."
            )
        state.log.write("handoff", outcome="complete", summary=summary)
        state.begin_closing()
        return "Done. If you haven't already, thank them and say goodbye in one short sentence, then stop talking."

    tools: list[Any] = [record_item, escalate, complete]

    # docs/escalation-handoff.md, part 1.
    tools.append(make_transfer_to_team_tool(state))
    return tools


# The warm introduction, read near-verbatim rather than freely paraphrased (see
# `_warm_introduction_instruction`): asking the model to "paraphrase your summary"
# after the tool returned was unreliable -- Nova Sonic sometimes said only the fixed
# pre-tool line and nothing else before the call ended. Composing the sentence in
# code and telling the model to say it "exactly, word for word" instead follows this
# codebase's
# own established pattern for anything that must reliably be said (OPENING, TRANSFER_NOTE):
# a model asked to repeat fixed text drops it far less often than one asked to compose
# fresh text from an instruction. The alternative considered -- delivering the join as a
# separate input turn via `agent.send(...)` instead of through the tool's own return
# value -- was not built: this codebase's tools have no reference to the running `agent`
# to call `send` on, and docs/escalation-handoff.md's part 3 already covers why a second
# "something happened" channel outside a tool's return value doesn't fit this codebase's
# pattern. Not yet re-verified against a fresh batch of live handoff calls in this
# session; re-run the deployed path's `--team-answer` batch (at least 5 calls) to
# confirm before relying on this.
def _warm_introduction_instruction(message: str) -> str:
    sentence = f"Thanks for picking this up. {message.strip()}"
    closing = "I'll let you two take it from here,"
    return (
        "A colleague has joined the call. Say this exactly, word for word, to "
        f'introduce them to the office: "{sentence}" Then say "{closing}" and stop '
        "talking. Your part of this call is done; do not call any more tools."
    )


def make_transfer_to_team_tool(state: CallState) -> Any:
    """`transfer_to_team`, LiveKit mode only (docs/escalation-handoff.md, parts 1-6).

    Waits (via `state.team_dialer`, up to `TEAM_HANDOFF_WAIT_SECONDS`) for a human to
    join the call before returning, so the string it hands back is always the model's
    very next move: a warm introduction once someone has joined, or the "no one
    answered" apology-then-escalate instruction if nobody did (part 5) -- never a
    separate "something happened" signal delivered later, which this codebase's tools
    have no channel for (see the doc's part 3).
    """

    @tool
    async def transfer_to_team(reason: str, summary: str) -> str:
        """Bring a colleague onto this call at a real dead end: the front desk refuses
        or cannot give you a missing item and offers no workable alternative, or asks
        to speak with someone on your own side.

        Being asked to hold, offered a transfer to another department, or told they'll
        go find or get someone else to help on their side is cooperation, not a dead
        end -- do not call this for that; hold quietly and continue when someone new
        comes on the line instead.

        Say "Let me bring in a colleague who can help with that." before calling this,
        word for word, then call it. Use this instead of escalate when the next useful
        step is a person joining this call right now, not this call ending with someone
        following up later.

        Only call this once per call. If you already called it once, do not call it
        again -- wait quietly instead, even if no one answered yet.

        Args:
            reason: why the call needs a person, one short phrase.
            summary: one or two sentences a colleague can act on without re-asking
                everything -- what the case needs, what's collected so far, what's
                still missing. Write it exactly the way you'd say it out loud: once
                they join, you will say this near-verbatim as your warm introduction,
                not paraphrase it.
        """
        if not reason or not reason.strip():
            return "A reason is required to bring in a colleague."
        if not summary or not summary.strip():
            return "A summary is required to bring in a colleague."
        if state.transfer_to_team_requested:
            # One handoff per call (docs/escalation-handoff.md): the model already said
            # its pre-tool line and asked once this call. A second call here, whether a
            # retry after "no answer" or a repeat while the first is still being dialed,
            # must not say the line again or ring the team twice -- just wait.
            return (
                "You already asked for a colleague on this call. Do not say that line "
                "again and do not call this tool again. If you are still waiting, say "
                'at most one short "one moment" and then wait quietly.'
            )
        state.transfer_to_team_requested = True
        await state.backend.transfer_to_team(
            case_id=state.case_id, call=state.call_uuid, reason=reason, summary=summary
        )
        state.log.write("handoff_requested", reason=reason, summary=summary)
        state.log.write("handoff_ringing")
        result = await state.team_dialer.request_handoff(
            room=state.call_uuid,
            summary=summary,
            reason=reason,
            items=state.record.get("items", {}),
        )
        if not result.joined:
            state.transfer_to_team_failed = True
            state.log.write("handoff_failed", reason="no answer")
            return (
                "No one was available. Apologize briefly, then say the review team will "
                "follow up instead, and call escalate yourself with that reason."
            )
        state.log.write("handoff_joined", identity=result.identity)
        state.log.write(
            "handoff",
            outcome="handed_off",
            reason=reason,
            summary=summary,
            identity=result.identity,
        )
        await state.backend.mark_handoff_joined(
            case_id=state.case_id,
            call=state.call_uuid,
            reason=reason,
            summary=summary,
            identity=result.identity,
        )
        state.handed_off = True
        state.begin_closing(next_phase="handed_off")
        # `result.briefing` overrides the model's own `summary` when the dialer composed
        # its own name-free sentence instead (docs/escalation-handoff.md's AWS "direct"
        # variant, `AwsTeamDialer._request_handoff_direct`); every other dialer leaves it
        # unset, so the model's own `summary` is what gets said, same as always.
        return _warm_introduction_instruction(result.briefing or summary)

    return transfer_to_team


# --------------------------------------------------------------------------
# Phase runner
# --------------------------------------------------------------------------


class ToolAudit(HookProvider):
    """Writes every tool call into the case record's evidence, with its timing.

    Each entry: when, which phase, the tool and its input, whether it succeeded, what it
    returned to the model, and how long it took. Also a `tool` event for the dashboard.
    """

    def __init__(self, state: CallState, phase: str) -> None:
        self._state = state
        self._phase = phase

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after)

    def _after(self, event: AfterToolCallEvent) -> None:
        result = event.result or {}
        texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
        entry = {
            "t": time.time(),
            "phase": self._phase,
            "tool": event.tool_use.get("name"),
            "input": event.tool_use.get("input"),
            "status": result.get("status"),
            "result": " ".join(t for t in texts if t)[:500],
            "ms": round((event.duration or 0) * 1000),
        }
        if event.exception is not None:
            entry["error"] = f"{type(event.exception).__name__}: {event.exception}"
        self._state.record.setdefault("tool_calls", []).append(entry)
        self._state.log.write(
            "tool", tool=entry["tool"], status=entry["status"], ms=entry["ms"], input=entry["input"]
        )


def _is_retryable_phase_error(exc: Exception) -> bool:
    """Transient Bedrock/connection failures worth reopening the phase's session for,
    rather than ending the call."""
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionError)):
        return True
    if type(exc).__name__ in RETRYABLE_ERROR_NAMES:
        return True
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code in RETRYABLE_ERROR_NAMES


NAVIGATE_RESUME_NOTE = """

YOU WERE DISCONNECTED AND RECONNECTED
The call dropped and reconnected mid-call; you are still listening to the same phone
menu, from wherever it is now. Keys already pressed on this call, in order: {pressed}.
Do not press any of them again -- just resume listening, and press keys for whatever
the menu says from here.
"""


def _navigate_resume_note(state: CallState) -> str:
    if not state.pressed_keys:
        pressed = "none yet"
    else:
        pressed = "; ".join(f"{p['digit']} ({p['option']})" for p in state.pressed_keys)
    return NAVIGATE_RESUME_NOTE.format(pressed=pressed)


INTAKE_RESUME_NOTE = """

YOU WERE DISCONNECTED AND RECONNECTED
The call dropped and reconnected mid-conversation; the person is still on the line.
Items already recorded -- do not ask about these again:
{items_block}

The conversation so far, oldest first:
{conversation_block}

Say exactly "Sorry, I lost you for a second," then continue from where you left off:
ask about the next missing item you don't already have above. Do not re-introduce
yourself, do not repeat your opening line, and do not ask again for anything already
recorded.
"""

# The seed that makes the resumed session speak the apology immediately (set as both
# `opening` and `seed` for run_phase -- see run_intake_phase), instead of waiting for the
# other side to speak first the way a fresh `full_call` transfer would.
RESUME_SEED = (
    "The call just reconnected after a brief disconnection; the person is still on the "
    'line. Say exactly: "Sorry, I lost you for a second," then continue the conversation '
    "from where you left off, asking about the next missing item you don't already have. "
    "Do not repeat your opening introduction."
)


def _intake_resume_note(state: CallState) -> str:
    items = state.record.get("items", {})
    if items:
        items_block = "\n".join(
            f"- {key}: {item.get('value')} "
            f"({'confirmed' if item.get('confirmed') else 'not yet confirmed'})"
            for key, item in items.items()
        )
    else:
        items_block = "(none recorded yet)"
    recent = state.record.get("transcript", [])[-8:]
    if recent:
        conversation_block = "\n".join(f"{t['who']}: {t['text']}" for t in recent)
    else:
        conversation_block = "(no conversation yet)"
    return INTAKE_RESUME_NOTE.format(items_block=items_block, conversation_block=conversation_block)


async def run_phase(
    conn: Transport,
    state: CallState,
    model: BedrockNovaSonicModel,
    system_prompt: str,
    tools: list[Any],
    *,
    opening: str | None,
    phase: str,
    seed: str | None = None,
) -> None:
    """Run one attempt at a phase's `BidiAgent` until a tool or hangup calls
    `state.finish_phase`, or the session itself fails.

    `BidiAgent.run()` drives the streams until one of them raises. A tool can't end the
    phase by calling `agent.stop()` (it runs inside the agent's own task group and would
    cancel itself), so `finish_phase` wakes the input stream, which raises `_PhaseEnd`;
    `run()` then stops the streams in order (the output stream finishes playing first) and
    closes the model session. The same path ends a phase on hangup, even if the model has
    gone quiet. Strands also restarts the Nova Sonic connection before its 8-minute limit.

    Any other exception (a Nova Sonic stream error, a dropped connection, ...) propagates
    to the caller -- `run_navigate_phase`/`run_intake_phase` decide whether that's worth
    reopening the phase for (`_is_retryable_phase_error`) or ending the call.
    """
    agent = BidiAgent(
        model=model, system_prompt=system_prompt, tools=tools, hooks=[ToolAudit(state, phase)]
    )
    inputs = [CallInputStream(state, opening=bool(opening), seed=seed)]
    outputs = [CallOutputStream(conn, state)]
    hold_watchdog = (
        asyncio.create_task(_intake_hold_watchdog(state, agent)) if phase == "intake" else None
    )
    try:
        await agent.run(inputs=inputs, outputs=outputs)
    except _PhaseEnd:
        pass
    finally:
        if hold_watchdog is not None:
            hold_watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hold_watchdog


async def _navigate_watchdog(state: CallState) -> None:
    """If navigate hears nothing at all for `NAVIGATE_WATCHDOG_SECONDS`, treat it as
    stuck (the model's stream, or the audio bridge -- a real menu starts talking almost
    immediately) and restart the phase, instead of waiting out Bedrock's own eventual
    error. Sets `state.watchdog_triggered` so `run_navigate_phase` knows this
    `finish_phase` means "restart", not "actually done".
    """
    loop = asyncio.get_running_loop()
    heard_count = len(state.heard)
    deadline = loop.time() + NAVIGATE_WATCHDOG_SECONDS
    while True:
        await asyncio.sleep(2.0)
        if state.done.is_set():
            return
        if len(state.heard) != heard_count:
            heard_count = len(state.heard)
            deadline = loop.time() + NAVIGATE_WATCHDOG_SECONDS
            continue
        if loop.time() >= deadline:
            logger.warning(
                "navigate_watchdog_no_transcript_restarting_phase",
                call_id=state.call_uuid,
                seconds=NAVIGATE_WATCHDOG_SECONDS,
            )
            state.watchdog_triggered = True
            state.finish_phase(None)
            return


async def _intake_hold_watchdog(state: CallState, agent: BidiAgent) -> None:
    """During the intake phase, if nothing new has been heard for
    `INTAKE_HOLD_NUDGE_SECONDS`, send the model a text turn saying so.

    Unlike `_navigate_watchdog`, this never ends or restarts the phase: a hold is
    legitimate, often long, and only the model can judge whether it has gone on too long.
    What it needs, and does not otherwise get, is a way to notice that time has passed at
    all while the office side stays completely silent -- without this, a hold that never
    gets a second utterance from the office is invisible to a model that only reacts to
    what it hears, so the stall rules already in its own system prompt can never trigger.
    Repeats every `INTAKE_HOLD_NUDGE_SECONDS` for as long as the silence continues, so a
    hold that just keeps not resolving keeps counting as the "more than once" the stall
    rules look for, instead of only ever reaching one.
    """
    loop = asyncio.get_running_loop()
    heard_count = len(state.heard)
    next_nudge = loop.time() + INTAKE_HOLD_NUDGE_SECONDS
    while True:
        await asyncio.sleep(2.0)
        if state.done.is_set():
            return
        if len(state.heard) != heard_count:
            heard_count = len(state.heard)
            next_nudge = loop.time() + INTAKE_HOLD_NUDGE_SECONDS
            continue
        if loop.time() >= next_nudge:
            logger.info(
                "intake_hold_watchdog_nudging_after_silence",
                call_id=state.call_uuid[:8],
                seconds=INTAKE_HOLD_NUDGE_SECONDS,
            )
            try:
                await agent.send(INTAKE_HOLD_NUDGE_TEXT)
            except Exception:
                logger.exception("intake_hold_watchdog_nudge_failed", call_id=state.call_uuid[:8])
                return
            next_nudge = loop.time() + INTAKE_HOLD_NUDGE_SECONDS


async def run_navigate_phase(conn: Transport, state: CallState) -> None:
    """Run the navigate phase, reopening its session up to `PHASE_RETRIES` times on a
    retryable Nova Sonic error or the no-transcript watchdog, before ending the call.
    """
    attempt = 0
    resume_note = ""
    while True:
        state.done = asyncio.Event()
        state.next_phase = None
        state.watchdog_triggered = False
        model = BedrockNovaSonicModel(
            model_id=MODEL_ID,
            boto_session=_BOTO_SESSION,
            voice=VOICE,
            audio={
                "input": {"sample_rate": INPUT_SAMPLE_RATE},
                "output": {"sample_rate": OUTPUT_SAMPLE_RATE},
            },
            params={"turnDetectionConfiguration": {"endpointingSensitivity": "LOW"}},
        )
        system_prompt = prompts.ivr_prompt(NAVIGATE_GOAL) + resume_note
        watchdog = asyncio.create_task(_navigate_watchdog(state))
        try:
            await run_phase(
                conn,
                state,
                model,
                system_prompt,
                make_navigate_tools(state),
                opening=None,
                phase="navigate",
            )
        except Exception as exc:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            if (
                attempt < PHASE_RETRIES
                and _is_retryable_phase_error(exc)
                and not state.hangup.is_set()
            ):
                attempt += 1
                state.log.write(
                    "retry", phase="navigate", attempt=attempt, error=f"{type(exc).__name__}: {exc}"
                )
                resume_note = _navigate_resume_note(state)
                continue
            logger.exception("phase_error", call_id=state.call_uuid)
            state.log.write("error", text=f"{type(exc).__name__}: {exc}")
            state.finish_phase(None)
            return
        else:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            if state.watchdog_triggered and not state.hangup.is_set():
                state.watchdog_triggered = False
                if attempt < PHASE_RETRIES:
                    attempt += 1
                    state.log.write(
                        "retry",
                        phase="navigate",
                        attempt=attempt,
                        error=f"no transcript for {NAVIGATE_WATCHDOG_SECONDS:.0f}s",
                    )
                    resume_note = _navigate_resume_note(state)
                    continue
                state.log.write(
                    "error",
                    text=f"navigate stalled: no transcript for {NAVIGATE_WATCHDOG_SECONDS:.0f}s, retries exhausted",
                )
            return


GREETING = re.compile(
    r"\b(hello|hi|hey|this is|speaking|how (can|may) i help|good (morning|afternoon|evening))\b"
)


def greeted_during_transfer(heard: list[str]) -> bool:
    """Did a person start talking after the transfer announcement, before intake began?

    Looks at what the menu phase heard last, after the final mention of "transfer", for
    the words people open a call with ("this is", "hello", "how can I help"...).
    """
    recent = " ".join(heard[-2:]).lower()
    cut = recent.rfind("transfer")
    return cut != -1 and bool(GREETING.search(recent[cut:]))


async def run_intake_phase(conn: Transport, state: CallState) -> None:
    """Run the intake phase, reopening its session up to `PHASE_RETRIES` times on a
    retryable Nova Sonic error before ending the call. A resumed attempt gets a short
    "lost you" opening plus a note of the items already recorded and the conversation
    so far, instead of re-greeting or re-asking for anything already collected.
    """
    attempt = 0
    resuming = False
    while True:
        state.done = asyncio.Event()
        state.next_phase = None
        model = BedrockNovaSonicModel(
            model_id=MODEL_ID,
            boto_session=_BOTO_SESSION,
            voice=VOICE,
            audio={
                "input": {"sample_rate": INPUT_SAMPLE_RATE},
                "output": {"sample_rate": OUTPUT_SAMPLE_RATE},
            },
            params={"turnDetectionConfiguration": {"endpointingSensitivity": INTAKE_ENDPOINTING}},
        )
        # Case-id-driven prompt (outbound_ivr.tools.rendering) when handle_call resolved a real
        # case (gateway + OUTBOUND_IVR_CASES_TABLE, or local's seeded demo case); otherwise the
        # fixed demo prompts.CASE, exactly as before. Not prompts.py itself: it's one of the
        # files this contract says not to edit (docs/design.md, "Ground
        # rules"). A case lookup that failed never gets here: handle_call ends the call
        # before any phase runs.
        case = state.case
        if case is not None:
            system_prompt = rendering.prior_auth_prompt_for(case)
            opening: str | None = rendering.opening_line_for(case)
            seed = rendering.opening_seed_for(case)[0]["content"]
        else:
            system_prompt = prompts.prior_auth_prompt()
            opening = prompts.opening_line()
            seed = prompts.opening_seed()[0]["content"]

        # transfer_to_team's own guidance (docs/escalation-handoff.md), same as the
        # tool itself in make_intake_tools.
        system_prompt += prompts.TRANSFER_TO_TEAM_NOTE

        if resuming:
            # A reconnect mid-conversation: say a short apology and carry on from what's
            # already recorded, instead of the transfer-wait/greeting logic below, which
            # only applies to a session that hasn't spoken to anyone yet.
            system_prompt += _intake_resume_note(state)
            opening = RESUME_SEED
            seed = RESUME_SEED
        elif state.mode == "full_call":
            system_prompt += TRANSFER_NOTE.format(opening=opening)
            if greeted_during_transfer(state.heard):
                # A fast front desk spoke over the end of the transfer message, so the menu
                # phase heard the greeting; waiting for one would wait forever. Open now.
                logger.info(
                    "front_desk_already_greeted_during_transfer", call_id=state.call_uuid[:8]
                )
            else:
                opening = None

        try:
            await run_phase(
                conn,
                state,
                model,
                system_prompt,
                make_intake_tools(state),
                opening=opening,
                phase="intake",
                seed=seed,
            )
            return
        except Exception as exc:
            if (
                attempt < PHASE_RETRIES
                and _is_retryable_phase_error(exc)
                and not state.hangup.is_set()
            ):
                attempt += 1
                state.log.write(
                    "retry", phase="intake", attempt=attempt, error=f"{type(exc).__name__}: {exc}"
                )
                resuming = True
                continue
            logger.exception("phase_error", call_id=state.call_uuid)
            state.log.write("error", text=f"{type(exc).__name__}: {exc}")
            state.finish_phase(None)
            return


# --------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------


# How long BOTH legs of a handed-off call can go completely silent before the call is
# treated as over. Not a conversational pause: a real call still carries RTP while nobody
# speaks, so this only elapses when the media stream itself has stopped.
#
# Why this exists at all: after a handoff Outbound IVR stays in the room and waits for
# `state.hangup`, which is only ever set once the far end's frames stop arriving, which
# is delegated entirely to livekit-sip's own no-RTP watchdog. On a measured real call
# that watchdog took 120 seconds from the front desk actually hanging up (confirmed
# against the Connect contact record, the livekit-sip container log and the events
# table, all three agreeing), during which the human on the Evidence leg sat on a dead
# call.
#
# That 120 seconds is now known to have been our own `SIP_MEDIA_TIMEOUT_S`, which the
# deployed image does honour per call, so the primary fix lives there: the AWS runtime
# sets it to 15 seconds and livekit-sip removes the dead participant itself, which
# arrives here as a real `participant_disconnected`. This stays as the net for when that
# does not happen, and the default is deliberately long enough that it cannot plausibly
# interrupt a working conversation -- three measured calls were all ended by this at
# 20-27 seconds when it was 20 and watched only the front desk, one of them while the
# operator believed the call was live.
HANDOFF_SILENCE_TIMEOUT_S = float(os.environ.get("OUTBOUND_IVR_HANDOFF_SILENCE_TIMEOUT_S", "45"))
_HANDOFF_SILENCE_POLL_S = 1.0

# RMS below which a far-end frame counts as no sound at all. A live phone line carries a
# noise floor well above this even when nobody is speaking; the silence livekit-sip
# publishes once inbound RTP stops sits at or near zero. Low on purpose: the job is to
# notice a dead stream, not to detect a pause in conversation.
# 16-bit PCM, matching outbound_ivr.voice.call_recorder.SAMPLE_WIDTH and the 320-byte
# frame contract every transport shares.
_SAMPLE_WIDTH = 2
FAR_AUDIO_SILENCE_RMS = int(os.environ.get("OUTBOUND_IVR_FAR_AUDIO_SILENCE_RMS", "50"))


async def _wait_for_handoff_to_end(state: CallState, transport: Transport | None = None) -> str:
    """Block until the handed-off call is over. Returns why it ended.

    Races the real hangup signal against BOTH legs of the call going silent, and keys the
    timeout off the LAST AUDIO FRAME rather than time since the handoff. That difference
    matters: on the measured call the three-way conversation genuinely continued for
    about 19 seconds after Outbound IVR's warm introduction finished, so a timer started at
    handoff would have cut off a working call.

    Both legs, because the front desk alone is not the conversation. The team member's
    audio never reaches `state.last_far_audio` -- it is deliberately not fed to the
    queue Outbound IVR listens on, since Outbound IVR's own session has ended by then (see
    `livekit_transport._pump_team_audio`) -- so watching the front desk alone could not
    tell a front-desk person listening quietly from one who had hung up. Three measured
    calls all ended here at 20-27 seconds for exactly that reason, one of them while the
    operator believed the call was still live. A transport with no team leg has no
    `last_team_audio` and falls back to the front desk alone, unchanged.
    """
    while True:
        try:
            await asyncio.wait_for(state.hangup.wait(), timeout=_HANDOFF_SILENCE_POLL_S)
            return "far end hung up"
        except TimeoutError:
            last_sound = max(state.last_far_audio, getattr(transport, "last_team_audio", 0.0))
            silence = time.monotonic() - last_sound
            if silence >= HANDOFF_SILENCE_TIMEOUT_S:
                logger.warning(
                    "handoff_silence_ending_call",
                    call_id=state.call_uuid,
                    silence_seconds=round(silence, 1),
                )
                state.log.write("status", value="handoff-silence", seconds=round(silence, 1))
                return "handoff went silent"


async def read_audio(transport: Transport, state: CallState) -> None:
    """Fan incoming frames into `state.raw_audio`, until hangup or EOF."""
    try:
        async for frame in transport.frames():
            if frame.type == frames.MsgType.AUDIO:
                # Stamped only when the frame carries actual SOUND, not merely when a
                # frame arrives. livekit-sip keeps publishing an audio track into the
                # room after the remote stops sending RTP, so frames never stop: a
                # measured call had them arriving for the full two minutes between the
                # front desk hanging up and livekit-sip's own media timeout killing the
                # participant. A frames-arriving check can therefore never fire, which
                # is exactly how the first version of this missed.
                #
                # Digital silence from that bridge is at or near zero RMS, while a live
                # G.711 line always carries some noise floor, so the threshold is
                # deliberately low: it catches a dead stream, not a quiet speaker.
                if audioop.rms(frame.payload, _SAMPLE_WIDTH) > FAR_AUDIO_SILENCE_RMS:
                    state.last_far_audio = time.monotonic()
                await state.raw_audio.put(frame.payload)
                if state.levels:
                    state.levels.feed_far(frame.payload)
            elif frame.type == frames.MsgType.DTMF:
                digit = frame.payload.decode("ascii", "replace") if frame.payload else ""
                logger.info("dtmf_received_from_caller", call_id=state.call_uuid, digit=digit)
            elif frame.type == frames.MsgType.ERROR:
                text = frame.payload.decode("utf-8", "replace")
                state.log.write("error", text=text)
    except (frames.FrameError, TransportError) as exc:
        state.log.write("error", text=str(exc))
    finally:
        state.hangup.set()
        state.finish_phase(None)


def write_case_record(state: CallState) -> None:
    """Store the case record, even for a call that never finished, through the shared
    `outbound_ivr.tools.store.ArtifactStore` interface.

    `OUTBOUND_IVR_ARTIFACTS_BUCKET` set (always true in the deployed Runtime):
    s3://OUTBOUND_IVR_ARTIFACTS_BUCKET/cases/<case_id>/<call_uuid>.json -- a key per case_id,
    not per pa_reference, because the dashboard API (lambdas/api/handler.py) lists
    `cases/{case_id}/` to find a case's calls. Otherwise falls back to
    runs/cases/<pa_reference>-<call_uuid>.json on local disk.

    A failed S3 put (throttling, an expired role session, a bucket policy change) falls
    back to that same local path rather than losing the record, and logs both the S3 key
    it meant to write and where the body actually went, so it can be recovered by hand.
    """
    state.record["ended_at"] = datetime.now(UTC).isoformat()
    body = json.dumps(state.record, indent=2).encode("utf-8")
    file_name = f"{state.record['pa_reference']}-{state.call_uuid}.json"
    bucket = os.environ.get("OUTBOUND_IVR_ARTIFACTS_BUCKET")
    if bucket:
        case_id = state.case_id or state.record["pa_reference"]
        s3_key = f"cases/{case_id}/{state.call_uuid}.json"
        try:
            location = S3ArtifactStore().put(s3_key, body, content_type="application/json")
        except Exception:
            location = LocalArtifactStore(root=RUNS / "cases").put(
                file_name, body, content_type="application/json"
            )
            logger.warning(
                "case_record_s3_write_failed",
                call_id=state.call_uuid,
                bucket=bucket,
                s3_key=s3_key,
                fallback_location=location,
                exc_info=True,
            )
            return
    else:
        store = LocalArtifactStore(root=RUNS / "cases")
        location = store.put(file_name, body, content_type="application/json")
    logger.info("wrote_case_record", call_id=state.call_uuid, location=location)


def upload_recording(transport: Transport) -> None:
    """Upload the call's finished WAV to s3://OUTBOUND_IVR_ARTIFACTS_BUCKET/recordings/
    <call_uuid>.wav -- the key `lambdas/api/handler.py`'s presigned GET already
    expects -- once the transport has actually closed (so the file is finalized).

    Gated on `OUTBOUND_IVR_ARTIFACTS_BUCKET` being set *and* the transport actually having a
    recording (`recording_path`, only set by `LiveKitTransport`).
    """
    bucket = os.environ.get("OUTBOUND_IVR_ARTIFACTS_BUCKET")
    path = getattr(transport, "recording_path", None)
    if not bucket or not path:
        return
    try:
        if not path.exists():
            return
        body = path.read_bytes()
    except OSError:
        logger.warning(
            "recording_read_failed", call_id=transport.call_uuid, path=str(path), exc_info=True
        )
        return
    try:
        location = S3ArtifactStore().put(
            f"recordings/{transport.call_uuid}.wav", body, content_type="audio/wav"
        )
    except Exception:
        logger.warning("recording_upload_failed", call_id=transport.call_uuid, exc_info=True)
        return
    logger.info("uploaded_recording", call_id=transport.call_uuid, location=location)


async def handle_call(transport: Transport) -> None:
    """Run one call end to end over `transport`: `agentcore/livekit_app.py` builds a
    `LiveKitTransport` and calls this directly."""
    try:
        call_uuid = await transport.handshake()
    except (frames.FrameError, TransportError) as exc:
        logger.warning("handshake_failed", peer=transport.peer, error=str(exc))
        return

    logger.info("call_connected", call_id=call_uuid, peer=transport.peer)
    mode = transport.mode
    # What this always resolved to once TRANSPORT_MODE was "livekit", not "agentcore"
    # (the now-removed dead branch this replaces): the DynamoDB forward below still
    # carries events to the dashboard either way, so this is unchanged behavior, not a
    # fix -- changing it to "stdout" is a separate decision for a later release.
    sink = "file"
    # Events -> DynamoDB (lambdas/api/handler.py's own item shape) is an additional
    # forward target, gated on OUTBOUND_IVR_EVENTS_TABLE being set -- see _DynamoDBEventSink's
    # own docstring.
    events_table = os.environ.get("OUTBOUND_IVR_EVENTS_TABLE")
    forward = _combine_forwards(
        transport.send_event, _DynamoDBEventSink(events_table).write if events_table else None
    )
    log = EventLog(call_uuid, sink=sink, forward=forward)
    state = CallState(
        call_uuid=call_uuid, mode=mode, log=log, transport=transport, case_id=transport.case_id
    )
    state.backend = make_backend(state.record, state.case_id)
    # transfer_to_team's own backend (docs/escalation-handoff.md): waits for a
    # "team-" participant via this call's own transport -- or, OUTBOUND_IVR_TEAM_DIALER=aws
    # (always set by the deployed Runtime), dials a real SIP leg to
    # OUTBOUND_IVR_EVIDENCE_NUMBER through Amazon Connect instead (same doc, "AWS").
    state.team_dialer = AwsTeamDialer(
        join_waiter=transport.wait_for_team_join,
        press_key=transport.press_key,
        case_id=state.case_id,
    )
    state.record.update(mode=mode, model=MODEL_ID, voice=VOICE)
    if state.case_id:
        state.record.setdefault("case_id", state.case_id)
    if LEVELS_ENABLED:
        state.levels = LevelMeter(call_uuid)
        state.levels.start()

    # Resolved once per call (not per phase): a real case store (gateway + OUTBOUND_IVR_CASES_TABLE)
    # is read once, and a missing case ends the call before any Nova Sonic session opens --
    # never fall back to the fixed demo case by mistake once a real store is configured.
    case_lookup_failed = False
    try:
        state.case = state.backend.case_for_prompt()
        if state.case is not None:
            intake.use_case(state.record, state.case)
    except CaseNotFoundError as exc:
        logger.error("case_not_found", call_id=call_uuid, error=str(exc))
        log.write("error", text=str(exc))
        case_lookup_failed = True
    except ClientError as exc:
        # DynamoDB unreachable, throttled, access denied, ...: same outcome as a case
        # that isn't there -- never guess and proceed with the demo case.
        logger.error("case_lookup_failed", call_id=call_uuid, error=str(exc))
        log.write("error", text=f"case lookup failed: {exc}")
        case_lookup_failed = True

    case_summary = (
        rendering.case_summary_for(state.case) if state.case is not None else prompts.case_summary()
    )
    checklist = (
        rendering.checklist_for(state.case) if state.case is not None else prompts.checklist()
    )
    reader_task = asyncio.create_task(read_audio(transport, state))
    log.write("ready", mode=mode, model=MODEL_ID, case=case_summary, checklist=checklist)

    # No case to work from: end the call politely (hang up in the `finally` below)
    # rather than start a phase with nothing real to say.
    phase: str | None = (
        None if case_lookup_failed else ("navigate" if mode == "full_call" else "intake")
    )
    # Bound here, not in the handoff branch: the `finally` reads it, and the branch is
    # skipped entirely when the far end hangs up during the warm introduction.
    handoff_end_reason: str | None = None
    try:
        while phase is not None:
            if state.hangup.is_set():
                # The call ended in the gap between phases; don't open a new Nova Sonic
                # session (and its pump) against a connection that's already gone.
                break
            if phase == "handed_off":
                # transfer_to_team ended the intake phase this way (docs/escalation-
                # handoff.md, part 4): stop running phases, but Outbound IVR doesn't hang up --
                # see the "handed_off" handling below.
                break
            state.done = asyncio.Event()
            state.next_phase = None
            log.write("phase", phase=phase)
            if phase == "navigate":
                await run_navigate_phase(transport, state)
            else:
                await run_intake_phase(transport, state)
            phase = state.next_phase
        if state.handed_off and not state.hangup.is_set():
            # Outbound IVR's transport goes silent but stays in the room (part 4, step 4); the
            # call itself ends when the far end or the team leaves, which `read_audio`
            # (via the transport's own `_end_call`) will set `state.hangup` for, exactly
            # as a real hangup does today.
            stop_publishing = getattr(transport, "stop_publishing", None)
            if stop_publishing is not None:
                with contextlib.suppress(Exception):
                    await stop_publishing()
            # Not a bare `await state.hangup.wait()`: that waits on livekit-sip's own
            # no-RTP watchdog, which measured 120 seconds on a real call while a human
            # sat on a dead line. See `_wait_for_handoff_to_end`.
            handoff_end_reason = await _wait_for_handoff_to_end(state, transport)
    finally:
        # Decide the reason before stopping the reader: stopping it sets `hangup`.
        far_end_hung_up = state.hangup.is_set()
        if not far_end_hung_up and not state.handed_off:
            # The call may already be gone (a LiveKit room deleted by End call): hanging
            # up must not stop the "ended" event and the case record below.
            try:
                await transport.hang_up()
            except Exception:
                logger.warning(
                    "hang_up_failed_call_probably_already_gone", call_id=call_uuid, exc_info=True
                )
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader_task
        if state.handed_off:
            reason = handoff_end_reason or "handed off"
        else:
            reason = "far end hung up" if far_end_hung_up else "agent hung up"
        log.write("ended", reason=reason)
        if state.team_dialer is not None:
            # The call is over: an Evidence team card still showing ringing/connected
            # for it (e.g. the far end hung up before anyone joined, or while the team
            # was on the call) must drop back to idle (docs/escalation-handoff.md).
            with contextlib.suppress(Exception):
                await state.team_dialer.clear_handoff(call_uuid)
        # Close the transport FIRST. `write_case_record` does a synchronous S3 PutObject
        # with no `to_thread`, which measured 5.5 seconds on a real call, and until
        # `transport.close()` runs the room stays up, which on a handoff means the human
        # on the Evidence leg is still holding a live call to nobody. The record and the
        # recording are not time-critical; releasing the room is.
        await transport.close()
        # Guarded like every other step here: a failed write (S3 and its local fallback
        # both) must not skip the recording upload, the backend's aclose, or closing the
        # call log and level meter below.
        try:
            write_case_record(state)
        except Exception:
            logger.warning("case_record_write_failed", call_id=call_uuid, exc_info=True)
        upload_recording(transport)
        # GatewayBackend holds one MCP session open for the whole call (see its
        # own comment); release it here. LocalBackend has no aclose and needs none.
        backend_aclose = getattr(state.backend, "aclose", None)
        if backend_aclose is not None:
            with contextlib.suppress(Exception):
                await backend_aclose()
        log.close()
        if state.levels:
            await state.levels.close()
        logger.info("call_ended", call_id=call_uuid)


def _env_flag(name: str) -> bool:
    """True if the named env var is set to something other than empty/0/false/no."""
    return os.environ.get(name, "") not in ("", "0", "false", "False", "no")


def _configure_telemetry() -> None:
    """Wire Strands' OpenTelemetry spans (session, connection, each model response,
    barge-ins; scope `strands.telemetry.tracer`) to wherever they should go.

    Two independent triggers, either or both may fire:

    - `OUTBOUND_IVR_OTEL=console` prints spans to stdout; `OUTBOUND_IVR_OTEL=otlp` sends them to
      `OTEL_EXPORTER_OTLP_ENDPOINT`, e.g. a local Jaeger on http://localhost:4318. Local
      dev/manual use only.
    - `AGENT_OBSERVABILITY_ENABLED` is set by the AgentCore Runtime platform itself
      (never set it in CDK/Dockerfiles) once the container is launched under the
      `opentelemetry-instrument` wrapper (see agentcore/Dockerfile*): ADOT has already
      configured the *global* tracer provider by the time this runs, exporting to
      X-Ray/CloudWatch. Strands must be handed that same provider so its spans are
      exported too -- a `StrandsTelemetry()` built with no `tracer_provider` creates
      its own, orphaned, never-exported provider, which is why this must not be gated
      on `OUTBOUND_IVR_OTEL` alone (that gate left Strands spans missing in production).
    """
    otel = os.environ.get("OUTBOUND_IVR_OTEL", "")
    observability_enabled = _env_flag("AGENT_OBSERVABILITY_ENABLED")
    if not otel and not observability_enabled:
        return

    from strands.telemetry import StrandsTelemetry

    if observability_enabled:
        from opentelemetry import trace as otel_trace

        telemetry = StrandsTelemetry(tracer_provider=otel_trace.get_tracer_provider())
    else:
        telemetry = StrandsTelemetry()
    if otel == "console":
        telemetry.setup_console_exporter()
    elif otel == "otlp":
        telemetry.setup_otlp_exporter()
    elif otel:
        raise SystemExit(f"OUTBOUND_IVR_OTEL must be console or otlp, not {otel!r}")
    logger.info(
        "opentelemetry_spans_configured",
        otel=otel or "off",
        agent_observability_enabled=observability_enabled,
    )
