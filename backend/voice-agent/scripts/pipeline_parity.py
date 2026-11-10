#!/usr/bin/env python3
"""Pipeline parity harness: drive the real ECS voice pipeline offline.

Builds the production pipeline with ``app.pipeline_ecs.create_voice_pipeline``
(same processors, observers, tools, aggregators and MetricsCollector) and
replaces only the edges that need a network:

- transport: a fake ``DailyTransport`` built on pipecat's real
  ``BaseInputTransport``/``BaseOutputTransport`` (so audio chunking, bot
  speaking frames and interruption handling are pipecat's own). Input streams
  20 ms of near-silent 8 kHz audio in real time; output paces audio in real
  time, the way a Daily call does.
- STT: a scripted processor that emits VAD start/stop, interim and final
  ``TranscriptionFrame``s (Deepgram SageMaker-shaped ``result``).
- TTS: the app's real ``DeepgramSageMakerTTSService`` with a fake BiDi client
  that returns PCM audio sized to the text.
- LLM: the REAL Bedrock model (needs AWS credentials).

The script plays a call: greeting, a normal question, a tool call
(``get_current_time``), and a barge-in while bot audio is playing, then the
caller hangs up. It records what reaches the transport output, the
interruption behaviour, the tool call and its context entries, the final LLM
context, every EMF record printed by ``app/observability.py``, and teardown
hygiene.

Usage (run from backend/voice-agent with the venv under test):

    python scripts/pipeline_parity.py capture --out /tmp/parity-1.6.json
    python scripts/pipeline_parity.py diff /tmp/parity-0.0.108.json /tmp/parity-1.6.json

By default the scripted STT injects VAD start/stop frames and the input audio
is near-silent. For realistic turn-taking, synthesize the caller's lines once
with Amazon Polly and pass them to every capture; pipecat's own Silero VAD and
Smart Turn then decide turn start/stop on real speech:

    python scripts/pipeline_parity.py synth --speech-dir /tmp/parity-speech
    python scripts/pipeline_parity.py capture --speech-dir /tmp/parity-speech --out ...

``diff`` exits non-zero if any EMF metric name, unit, namespace or dimension
set differs, or if a behavioural check fails in either capture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import struct
import sys
import time
import warnings
from collections import Counter
from importlib import metadata
from typing import Any, Dict, List, Optional
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # backend/voice-agent

SAMPLE_RATE = 8000
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Frames that repeat continuously; counted, not listed in the sequence.
PERIODIC_FRAMES = {"InputAudioRawFrame", "UserSpeakingFrame", "BotSpeakingFrame"}

TURNS = [
    ("question", "In one short sentence, what colour is the sky on a clear day?"),
    ("tool", "What time is it right now?"),
    (
        "barge_in_prompt",
        "Tell me a long story about a friendly dragon. Make it at least eight sentences.",
    ),
]
BARGE_IN_TEXT = "Sorry, stop there. Actually, can you just say hello instead?"


# ---------------------------------------------------------------------------
# Fakes (defined lazily so pipecat is only imported after env setup)
# ---------------------------------------------------------------------------


def build_fakes(rec: "Recorder"):
    from pipecat.frames.frames import (
        DataFrame,
        Frame,
        InputAudioRawFrame,
        InterimTranscriptionFrame,
        StartFrame,
        TranscriptionFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport

    try:
        from pipecat.utils.time import time_now_iso8601
    except ImportError:  # pragma: no cover
        def time_now_iso8601():
            return time.strftime("%Y-%m-%dT%H:%M:%S")

    class UserUtterance(DataFrame):
        """Harness control frame: the caller says ``text``.

        With ``audio`` (16-bit 8 kHz PCM), the fake input transport streams it
        in real time so the aggregator's own VAD and Smart Turn see speech, and
        the scripted STT only supplies transcripts. Without it, the scripted
        STT injects VAD start/stop frames around the transcripts.
        """

        def __init__(self, text: str, audio: Optional[bytes] = None):
            super().__init__()
            self.text = text
            self.audio = audio

    class FakeInput(BaseInputTransport):
        async def start(self, frame: StartFrame):
            await super().start(frame)
            await self.set_transport_ready(frame)
            self._feed = self.create_task(self._feed_audio())

        async def _feed_audio(self):
            # 20 ms frames: queued caller speech if any, otherwise very quiet
            # noise so VAD stays quiet but RMS is finite.
            n = SAMPLE_RATE // 50
            quiet = b"".join(struct.pack("<h", (i % 7) - 3) for i in range(n))
            self._speech = getattr(self, "_speech", bytearray())
            while True:
                if self._speech:
                    chunk = bytes(self._speech[: 2 * n]).ljust(2 * n, b"\x00")
                    del self._speech[: 2 * n]
                else:
                    chunk = quiet
                await self.push_audio_frame(
                    InputAudioRawFrame(audio=chunk, sample_rate=SAMPLE_RATE, num_channels=1)
                )
                await asyncio.sleep(0.02)

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            if isinstance(frame, UserUtterance) and frame.audio:
                self._speech = getattr(self, "_speech", bytearray())
                self._speech.extend(frame.audio)
            await super().process_frame(frame, direction)

        async def _stop_feed(self):
            feed = getattr(self, "_feed", None)
            if feed:
                await self.cancel_task(feed)
                self._feed = None

        async def stop(self, frame):
            await self._stop_feed()
            await super().stop(frame)

        async def cancel(self, frame):
            await self._stop_feed()
            await super().cancel(frame)

    class FakeOutput(BaseOutputTransport):
        async def start(self, frame: StartFrame):
            await super().start(frame)
            await self.set_transport_ready(frame)

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            if direction == FrameDirection.DOWNSTREAM:
                rec.output_frame(frame)
            await super().process_frame(frame, direction)

        async def write_audio_frame(self, frame) -> bool:
            rec.audio_written(len(frame.audio))
            # Pace like a real call: 16-bit mono PCM at the output rate.
            await asyncio.sleep(len(frame.audio) / (2 * frame.sample_rate))
            return True

    class FakeDailyTransport(BaseTransport):
        """Stands in for DailyTransport(room_url, token, bot_name, params=...)."""

        def __init__(self, room_url, token, bot_name, params, **kwargs):
            super().__init__(**kwargs)
            self._params = params
            self._input = FakeInput(params, name="FakeDailyInput")
            self._output = FakeOutput(params, name="FakeDailyOutput")
            for event in (
                "on_joined",
                "on_first_participant_joined",
                "on_participant_left",
                "on_dialin_ready",
                "on_dialin_connected",
                "on_dialin_stopped",
                "on_dialin_warning",
                "on_dialin_error",
            ):
                self._register_event_handler(event)

        def input(self):
            return self._input

        def output(self):
            return self._output

        def participants(self):
            return {"caller": {"id": "caller"}}

        async def fire(self, event: str, *args):
            # _call_event_handler prepends the transport itself.
            await self._call_event_handler(event, *args)

    class ScriptedSTT(FrameProcessor):
        """Turns UserUtterance control frames into VAD + transcription frames."""

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, UserUtterance):
                if frame.audio:
                    self.create_task(self._transcribe(frame.text, len(frame.audio) / (2 * SAMPLE_RATE)))
                else:
                    self.create_task(self._speak(frame.text))
                return
            await self.push_frame(frame, direction)

        async def _transcribe(self, text: str, duration: float):
            """Speech mode: interim mid-utterance, final ~300 ms after speech ends."""
            rec.mark("user_speech_start", text=text, audio_secs=round(duration, 2))
            await asyncio.sleep(duration / 2)
            half = " ".join(text.split()[: max(1, len(text.split()) // 2)])
            await self.push_frame(
                InterimTranscriptionFrame(text=half, user_id="caller", timestamp=time_now_iso8601())
            )
            await asyncio.sleep(duration / 2 + 0.3)
            result = {
                "type": "Results",
                "is_final": True,
                "channel": {"alternatives": [{"transcript": text, "confidence": 0.97}]},
            }
            await self.push_frame(
                TranscriptionFrame(
                    text=text, user_id="caller", timestamp=time_now_iso8601(), result=result
                )
            )
            rec.mark("user_speech_final", text=text)

        async def _speak(self, text: str):
            rec.mark("user_speech_start", text=text)
            # start/stop secs match the app's SileroVADAnalyzer(VADParams(stop_secs=0.3)).
            await self.push_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
            await asyncio.sleep(0.3)
            half = " ".join(text.split()[: max(1, len(text.split()) // 2)])
            await self.push_frame(
                InterimTranscriptionFrame(text=half, user_id="caller", timestamp=time_now_iso8601())
            )
            await asyncio.sleep(0.3)
            await self.push_frame(VADUserStoppedSpeakingFrame(stop_secs=0.3))
            await asyncio.sleep(0.15)
            result = {
                "type": "Results",
                "is_final": True,
                "channel": {"alternatives": [{"transcript": text, "confidence": 0.97}]},
            }
            await self.push_frame(
                TranscriptionFrame(
                    text=text, user_id="caller", timestamp=time_now_iso8601(), result=result
                )
            )
            rec.mark("user_speech_final", text=text)

    class _Result:
        def __init__(self, data: bytes):
            self.value = type("P", (), {"bytes_": data})()

    class FakeTTSBidiClient:
        """Fake SageMakerBidiClient for the app's Deepgram TTS (v1/speak)."""

        WORD_SECS = 0.12
        TTFB_SECS = 0.08

        def __init__(self, **kwargs):
            self.bidi_endpoint = "https://fake-sagemaker.invalid:8443"
            self.is_active = False
            self._queue: asyncio.Queue = asyncio.Queue()
            self._pending: List[asyncio.Task] = []

        async def start_session(self):
            self.is_active = True

        async def send_json(self, data: Dict[str, Any]):
            kind = data.get("type")
            rec.tts_message(kind)
            if kind == "Speak":
                words = max(1, len(data.get("text", "").split()))
                nbytes = int(words * self.WORD_SECS * SAMPLE_RATE) * 2
                self._schedule(self.TTFB_SECS, _Result(b"\x01\x00" * (nbytes // 2)))
            elif kind == "Flush":
                self._schedule(self.TTFB_SECS + 0.01, _Result(json.dumps({"type": "Flushed"}).encode()))
            elif kind == "Clear":
                for t in self._pending:
                    t.cancel()
                self._pending.clear()
                self._schedule(0.01, _Result(json.dumps({"type": "Cleared"}).encode()))

        def _schedule(self, delay: float, item):
            async def deliver():
                await asyncio.sleep(delay)
                await self._queue.put(item)

            task = asyncio.get_running_loop().create_task(deliver())
            self._pending.append(task)
            task.add_done_callback(lambda t: t in self._pending and self._pending.remove(t))

        async def receive_response(self):
            return await self._queue.get()

        async def close_session(self):
            self.is_active = False
            for t in self._pending:
                t.cancel()
            self._pending.clear()

    return {
        "UserUtterance": UserUtterance,
        "FakeDailyTransport": FakeDailyTransport,
        "ScriptedSTT": ScriptedSTT,
        "FakeTTSBidiClient": FakeTTSBidiClient,
    }


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.t0 = time.perf_counter()
        self.output: List[Dict[str, Any]] = []
        self.periodic = Counter()
        self.audio_writes: List[float] = []
        self.audio_bytes: List[int] = []
        self.marks: List[Dict[str, Any]] = []
        self.tts_messages = Counter()
        self.emf: List[Dict[str, Any]] = []
        self.loguru: List[str] = []
        self.turn_log: List[str] = []

    def now(self) -> float:
        return round(time.perf_counter() - self.t0, 3)

    def output_frame(self, frame):
        name = type(frame).__name__
        if name in PERIODIC_FRAMES:
            self.periodic[name] += 1
            return
        self.output.append({"t": self.now(), "type": name})

    def audio_written(self, nbytes: int):
        self.audio_writes.append(self.now())
        self.audio_bytes.append(nbytes)

    def mark(self, event: str, **kw):
        self.marks.append({"t": self.now(), "event": event, **kw})

    def tts_message(self, kind: Optional[str]):
        self.tts_messages[kind or "?"] += 1

    def emf_print(self, *args, **kwargs):
        for a in args:
            try:
                rec = json.loads(a)
            except (TypeError, ValueError):
                continue
            if isinstance(rec, dict) and "_aws" in rec:
                self.emf.append(rec)

    def audio_since(self, t: float) -> int:
        return sum(1 for w in self.audio_writes if w > t)

    async def wait_bot_idle(self, idle: float = 1.5, timeout: float = 45.0, need_audio_after: Optional[float] = None):
        """Wait until bot audio has started (after ``need_audio_after``) and then stopped for ``idle``."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.1)
            if need_audio_after is not None and self.audio_since(need_audio_after) == 0:
                continue
            last = self.audio_writes[-1] if self.audio_writes else 0.0
            if self.now() - last >= idle:
                return True
        return False

    async def wait_output_frame(self, name_part: str, after: float, timeout: float) -> Optional[float]:
        """Wait for a frame whose type contains ``name_part`` to reach output; return its time."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            hit = next((f["t"] for f in self.output if f["t"] >= after and name_part in f["type"]), None)
            if hit is not None:
                return hit
            await asyncio.sleep(0.02)
        return None

    async def wait_audio_flowing(self, after: float, chunks: int, timeout: float = 45.0) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.audio_since(after) >= chunks:
                return True
            await asyncio.sleep(0.02)
        return False


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _collapse(seq: List[str]) -> List[str]:
    out: List[str] = []
    for name in seq:
        if out and out[-1].split(" x")[0] == name:
            base, _, n = out[-1].partition(" x")
            out[-1] = f"{base} x{int(n or 1) + 1}"
        else:
            out.append(name)
    return out


def _bursts(times: List[float], sizes: List[int], gap: float = 0.3) -> List[Dict[str, Any]]:
    """Group audio writes into speaking bursts: start, end, chunk count, audio seconds."""
    out: List[Dict[str, Any]] = []
    for t, n in zip(times, sizes):
        if out and t - out[-1]["end"] <= gap:
            b = out[-1]
            b["end"] = t
            b["chunks"] += 1
            b["audio_secs"] = round(b["audio_secs"] + n / (2 * SAMPLE_RATE), 3)
        else:
            out.append({"start": t, "end": t, "chunks": 1, "audio_secs": round(n / (2 * SAMPLE_RATE), 3)})
    return out


def _emf_schema(rec: Dict[str, Any]) -> Dict[str, Any]:
    cwm = rec["_aws"]["CloudWatchMetrics"]
    sets = []
    for d in cwm:
        sets.append(
            {
                "namespace": d.get("Namespace"),
                "dimensions": sorted(sorted(ds) for ds in d.get("Dimensions", [])),
                "metrics": sorted((m["Name"], m.get("Unit")) for m in d.get("Metrics", [])),
            }
        )
    dim_keys = sorted({k for d in cwm for ds in d.get("Dimensions", []) for k in ds})
    return {
        "metric_sets": sets,
        "dimension_values_present": {k: (k in rec) for k in dim_keys},
        "metric_values_present": sorted(
            m["Name"] for d in cwm for m in d.get("Metrics", []) if m["Name"] in rec
        ),
        # Values are informational only (live LLM, wall-clock timings).
        "values": {
            m["Name"]: rec.get(m["Name"]) for d in cwm for m in d.get("Metrics", []) if m["Name"] in rec
        },
    }


def _emf_kind(rec: Dict[str, Any]) -> str:
    names = {m["Name"] for d in rec["_aws"]["CloudWatchMetrics"] for m in d.get("Metrics", [])}
    if "ToolExecutionTime" in names:
        return "tool"
    if "TurnCount" in names or "CallDuration" in names:
        return "call"
    if "ActiveSessions" in names or "SessionStarted" in names or "SessionEnded" in names:
        return "session"
    return "turn"


def _summarise_context(messages: List[Any]) -> List[Dict[str, Any]]:
    out = []
    for m in messages:
        if not isinstance(m, dict):
            m = getattr(m, "__dict__", {"repr": repr(m)})
        role = m.get("role")
        content = m.get("content")
        parts: List[str] = []
        text = ""
        if isinstance(content, str):
            parts = ["text"]
            text = content
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    parts.append(c.get("type") or ",".join(sorted(c.keys())))
                    text += str(c.get("text", ""))
        if m.get("tool_calls"):
            parts.append("tool_calls:" + ",".join(tc.get("function", {}).get("name", "?") for tc in m["tool_calls"]))
        if role == "tool":
            parts.append("tool_result")
            text = str(content)
        out.append({"role": role, "parts": parts, "text": text[:160]})
    return out


async def run_capture(out_path: str, speech_dir: Optional[str] = None) -> Dict[str, Any]:
    """Run the scripted call; restores environment variables and loguru sinks afterwards."""
    from loguru import logger as loguru_logger

    saved_env = dict(os.environ)
    try:
        return await _run_capture(out_path, speech_dir)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        loguru_logger.remove()
        loguru_logger.add(sys.stderr)


async def _run_capture(out_path: str, speech_dir: Optional[str]) -> Dict[str, Any]:
    speech = _load_speech(speech_dir) if speech_dir else {}
    os.environ.setdefault("DAILY_API_KEY", "fake-daily-key")
    os.environ.setdefault("AWS_REGION", "us-east-1")
    os.environ["LLM_MODEL_ID"] = MODEL_ID
    os.environ.pop("TRANSFER_DESTINATION", None)

    from loguru import logger as loguru_logger

    rec = Recorder()
    loguru_logger.remove()
    loguru_logger.add(
        lambda msg: rec.loguru.append(
            f"{msg.record['level'].name}: {msg.record['name']}: {msg.record['message'][:300]}"
        ),
        level="WARNING",
    )
    # Which turn strategy started/stopped each user turn (pipecat DEBUG logs).
    loguru_logger.add(
        lambda msg: rec.turn_log.append(f"{rec.now()} {msg.record['message'][:200]}"),
        level="DEBUG",
        filter=lambda r: "speaking (strategy" in r["message"] or "turn stop timeout" in r["message"].lower()
        or "user turn" in r["message"].lower() and "timeout" in r["message"].lower(),
    )

    fakes = build_fakes(rec)
    import app.observability as observability
    import app.pipeline_ecs as pipeline_ecs
    import app.services.deepgram_sagemaker_tts as dg_tts
    import app.services.factory as factory

    collector = observability.create_metrics_collector(call_id="parity-call", session_id="parity-session")
    config = pipeline_ecs.PipelineConfig(
        room_url="https://fake.daily.co/parity",
        room_token="fake-token",
        session_id="parity-session",
        system_prompt="You are a helpful phone assistant. Keep answers short.",
        voice_id="aura-2-thalia-en",
        aws_region="us-east-1",
        dialin_settings=pipeline_ecs.DialinSettings(call_id="c", call_domain="d", sip_uri="sip:x@y"),
        stt_provider="sagemaker",
        tts_provider="sagemaker",
        stt_endpoint="fake-stt",
        tts_endpoint="fake-tts",
    )

    from pipecat.pipeline.runner import PipelineRunner

    result: Dict[str, Any] = {}
    caught: List[str] = []
    with patch.object(observability, "print", rec.emf_print, create=True), patch.object(
        pipeline_ecs, "DailyTransport", fakes["FakeDailyTransport"]
    ), patch.object(factory, "create_stt_service", lambda cfg: fakes["ScriptedSTT"](name="ScriptedSTT")), patch.object(
        dg_tts, "SageMakerBidiClient", fakes["FakeTTSBidiClient"]
    ), patch.object(pipeline_ecs, "_get_enable_tool_calling", lambda: True), patch.object(
        pipeline_ecs, "_get_enable_filler_phrases", lambda: True
    ), patch.object(
        # Deployed default (SSM enable-conversation-logging="true"); this observer
        # is the one that counts InterruptionCount.
        pipeline_ecs, "_get_enable_conversation_logging", lambda: True
    ), warnings.catch_warnings(record=True) as wlist:
        warnings.simplefilter("always")
        task, transport = await pipeline_ecs.create_voice_pipeline(config, collector)

        runner = PipelineRunner(handle_sigint=False)
        run_task = asyncio.create_task(runner.run(task))
        await asyncio.sleep(1.0)

        UserUtterance = fakes["UserUtterance"]

        async def say(text: str):
            await task.queue_frames([UserUtterance(text, speech.get(text))])

        rec.mark("participant_joined")
        t = rec.now()
        await transport.fire("on_first_participant_joined", {"id": "caller"})
        greet_ok = await rec.wait_bot_idle(need_audio_after=t)

        turn_ok = {}
        for label, text in TURNS[:2]:
            t = rec.now()
            await say(text)
            turn_ok[label] = await rec.wait_bot_idle(need_audio_after=t)

        # Barge-in: wait for the long answer's audio to be flowing, then talk.
        t = rec.now()
        await say(TURNS[2][1])
        flowing = await rec.wait_audio_flowing(after=t, chunks=30)
        await asyncio.sleep(0.5)
        t_barge = rec.now()
        audio_before_barge = rec.audio_since(t)
        await say(BARGE_IN_TEXT)
        # Wait for the interruption to reach the output transport (VAD needs a
        # moment of speech first), then for the bot's reply to the barge-in.
        t_int_seen = await rec.wait_output_frame("Interruption", after=t_barge, timeout=10.0)
        t_after = (t_int_seen if t_int_seen is not None else rec.now()) + 0.05
        turn_ok["barge_in"] = await rec.wait_bot_idle(need_audio_after=t_after)

        rec.mark("participant_left")
        teardown_start_warnings = len(wlist)
        t_end = rec.now()
        await transport.fire("on_participant_left", {"id": "caller"}, "hangup")
        try:
            await asyncio.wait_for(run_task, timeout=30)
            clean_exit = True
        except asyncio.TimeoutError:
            clean_exit = False
            await task.cancel()
        teardown_secs = round(rec.now() - t_end, 3)
        collector.finalize(status="completed")

        # emit_session_health is invoked by service_main around each call;
        # include it so the EMF schema covers every metric family.
        emf = getattr(collector, "_emf", None)
        if emf is not None and hasattr(emf, "emit_session_health"):
            try:
                emf.emit_session_health(active_sessions=0, error_count=0, error_category=None)
            except TypeError as e:
                caught.append(f"emit_session_health signature: {e}")

        await asyncio.sleep(0.2)
        leftover = [
            t.get_coro().__qualname__
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        teardown_warnings = [
            f"{w.category.__name__}: {str(w.message)[:200]}" for w in wlist[teardown_start_warnings:]
        ]
        all_warnings = Counter(f"{w.category.__name__}: {str(w.message)[:120]}" for w in wlist)

    # ---- context ----
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator

    def _walk(p):
        for c in getattr(p, "processors", None) or getattr(p, "_processors", None) or []:
            yield c
            yield from _walk(c)

    user_agg = next((p for p in _walk(task._pipeline) if isinstance(p, LLMUserAggregator)), None)
    messages = user_agg._context.get_messages() if user_agg else []
    context = _summarise_context(messages)

    # ---- interruption ----
    out_after = [f for f in rec.output if f["t"] >= t_barge]
    interruption = next((f for f in out_after if "Interruption" in f["type"]), None)
    t_int = interruption["t"] if interruption else None
    # Bot audio that was still written after the interruption reached output,
    # before the bot's reply to the barge-in started (next TTSStartedFrame).
    next_start = next(
        (f["t"] for f in rec.output if t_int is not None and f["t"] > t_int and f["type"] == "TTSStartedFrame"),
        None,
    )
    stale_audio = (
        sum(1 for w in rec.audio_writes if t_int < w and (next_start is None or w < next_start))
        if t_int is not None
        else None
    )

    seq_types = [f["type"] for f in rec.output]
    emf_records = [{"kind": _emf_kind(r), **_emf_schema(r)} for r in rec.emf]

    result = {
        "versions": {
            p: metadata.version(p)
            for p in ("pipecat-ai", "daily-python", "aws_sdk_sagemaker_runtime_http2")
            if _has_dist(p)
        },
        "python": sys.version.split()[0],
        "input_mode": "speech" if speech else "scripted_vad",
        "model": MODEL_ID,
        "checks": {
            "greeting_spoken": greet_ok,
            "turns_completed": turn_ok,
            "barge_in_audio_was_flowing": flowing and audio_before_barge > 0,
            "interruption_reached_output": interruption is not None,
            "clean_exit": clean_exit,
        },
        "interruption": {
            "frame_type": interruption["type"] if interruption else None,
            "latency_ms_from_user_speech": round((t_int - t_barge) * 1000) if t_int is not None else None,
            "audio_chunks_written_after_interruption": stale_audio,
            "audio_chunks_before_barge": audio_before_barge,
        },
        "tool": {
            "tool_call_in_context": any(any(p.startswith("tool_calls:get_current_time") for p in m["parts"]) for m in context),
            "tool_result_in_context": any("tool_result" in m["parts"] for m in context),
            "spoken_response_in_context": any(
                m["role"] == "assistant" and "currently" in m["text"] and "UTC" in m["text"] for m in context
            ),
            "tool_metrics_emitted": any(r["kind"] == "tool" for r in emf_records),
        },
        "bot_audio_bursts": _bursts(rec.audio_writes, rec.audio_bytes),
        "output_sequence": _collapse(seq_types),
        "output_type_counts": dict(Counter(seq_types)),
        "periodic_counts": dict(rec.periodic),
        "tts_messages": dict(rec.tts_messages),
        "context": context,
        "emf": emf_records,
        "emf_count_by_kind": dict(Counter(r["kind"] for r in emf_records)),
        "teardown": {
            "seconds": teardown_secs,
            "leftover_tasks": leftover,
            "warnings_during_teardown": teardown_warnings,
        },
        "warnings": dict(all_warnings),
        "pipecat_warnings_and_errors": rec.loguru,
        "turn_strategy_log": rec.turn_log,
        "harness_errors": caught,
        "marks": rec.marks,
    }
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    return result


def _has_dist(name: str) -> bool:
    try:
        metadata.version(name)
        return True
    except metadata.PackageNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _schema_key(r: Dict[str, Any]) -> str:
    return json.dumps(r["metric_sets"], sort_keys=True)


def diff(a_path: str, b_path: str) -> int:
    a = json.load(open(a_path))
    b = json.load(open(b_path))
    va, vb = a["versions"].get("pipecat-ai"), b["versions"].get("pipecat-ai")
    blockers: List[str] = []
    notes: List[str] = []
    lines: List[str] = [
        f"# Pipeline parity: pipecat-ai {va} vs {vb}",
        "",
        f"Input mode: {a.get('input_mode')} vs {b.get('input_mode')}",
        "",
    ]

    # --- EMF schema ---
    def schemas(x):
        out: Dict[str, Counter] = {}
        for r in x["emf"]:
            out.setdefault(r["kind"], Counter())[_schema_key(r)] += 1
        return out

    def union(x):
        out: Dict[str, Dict[str, set]] = {}
        for r in x["emf"]:
            u = out.setdefault(r["kind"], {"namespace": set(), "dimensions": set(), "metrics": set()})
            for ms in r["metric_sets"]:
                u["namespace"].add(ms["namespace"])
                u["dimensions"].add(json.dumps(ms["dimensions"]))
                u["metrics"].update(f"{n} ({unit})" for n, unit in ms["metrics"])
        return out

    sa, sb = schemas(a), schemas(b)
    ua, ub = union(a), union(b)
    lines.append("## EMF metrics: names, units, namespace, dimensions (blocking)")
    for kind in sorted(set(ua) | set(ub)):
        xa, xb = ua.get(kind), ub.get(kind)
        if xa is None or xb is None:
            blockers.append(f"EMF {kind} records only emitted by one version")
            lines.append(f"- {kind}: **only in {va if xa else vb}**")
            continue
        bad = [f for f in ("namespace", "dimensions", "metrics") if xa[f] != xb[f]]
        if not bad:
            lines.append(
                f"- {kind}: MATCH: {len(xa['metrics'])} metric name/unit pairs, dimension sets "
                f"{sorted(xa['dimensions'])}, {sum(sa[kind].values())} vs {sum(sb[kind].values())} records"
            )
        else:
            blockers.append(f"EMF {kind} {'/'.join(bad)} mismatch")
            lines.append(f"- {kind}: **MISMATCH** in {bad}")
            for f in bad:
                lines.append(f"  - only in {va}: {sorted(xa[f] - xb[f])}")
                lines.append(f"  - only in {vb}: {sorted(xb[f] - xa[f])}")
    lines.append("")
    lines.append("## EMF metric combinations per record (informational; data-dependent)")
    for kind in sorted(set(sa) | set(sb)):
        ka, kb = set(sa.get(kind, {})), set(sb.get(kind, {}))
        if ka == kb:
            lines.append(f"- {kind}: same {len(ka)} combination(s)")
        else:
            notes.append(f"{kind} metric combinations differ (see per-turn presence)")
            lines.append(f"- {kind}: combinations differ")
    # dimension values present on every record
    for x, v in ((a, va), (b, vb)):
        missing = [(r["kind"], k) for r in x["emf"] for k, ok in r["dimension_values_present"].items() if not ok]
        if missing:
            blockers.append(f"EMF records in {v} missing dimension values: {sorted(set(missing))}")
    # per-turn metric presence
    ta = [tuple(r["metric_values_present"]) for r in a["emf"] if r["kind"] == "turn"]
    tb = [tuple(r["metric_values_present"]) for r in b["emf"] if r["kind"] == "turn"]
    if ta == tb:
        lines.append(f"- per-turn metrics present: MATCH across {len(ta)} turns")
    else:
        notes.append("per-turn metric presence differs (values depend on what happened in the turn)")
        lines.append("- per-turn metrics present: DIFFERENT")
        for i in range(max(len(ta), len(tb))):
            x = ta[i] if i < len(ta) else None
            y = tb[i] if i < len(tb) else None
            if x != y:
                lines.append(f"  - turn {i + 1}: {va}={x} {vb}={y}")
    ca = [tuple(r["metric_values_present"]) for r in a["emf"] if r["kind"] != "turn"]
    cb = [tuple(r["metric_values_present"]) for r in b["emf"] if r["kind"] != "turn"]
    if ca != cb:
        notes.append("per-call/tool/session metric presence differs")
        lines.append(f"- call/tool/session metrics present: **DIFFERENT** {va}={ca} {vb}={cb}")
    else:
        lines.append("- call/tool/session metrics present: MATCH")
    lines.append("")

    # --- behaviour checks ---
    lines.append("## Call-level metric values (informational)")
    lines.append(f"| metric | {va} | {vb} |")
    lines.append("|---|---|---|")
    call_a = next((r for r in a["emf"] if r["kind"] == "call"), {"values": {}})
    call_b = next((r for r in b["emf"] if r["kind"] == "call"), {"values": {}})
    for k in sorted(set(call_a.get("values", {})) | set(call_b.get("values", {}))):
        lines.append(f"| {k} | {call_a.get('values', {}).get(k)} | {call_b.get('values', {}).get(k)} |")
    lines.append("")

    lines.append("## Behaviour checks")
    for x, v in ((a, va), (b, vb)):
        flat = dict(x["checks"])
        turns = flat.pop("turns_completed")
        flat.update({f"turn_{k}": ok for k, ok in turns.items()})
        flat.update({f"tool_{k}": ok for k, ok in x["tool"].items() if k != "spoken_response_in_context"})
        failed = [k for k, ok in flat.items() if not ok]
        lines.append(f"- {v}: {'all pass' if not failed else 'FAILED: ' + ', '.join(failed)}")
        if failed:
            blockers.append(f"{v} behaviour checks failed: {failed}")
    lines.append("")

    lines.append("## Interruption")
    lines.append(f"| | {va} | {vb} |")
    lines.append("|---|---|---|")
    for k in a["interruption"]:
        lines.append(f"| {k} | {a['interruption'][k]} | {b['interruption'][k]} |")
    lines.append("")

    lines.append("## Tool call and context")
    lines.append(f"| | {va} | {vb} |")
    lines.append("|---|---|---|")
    for k in a["tool"]:
        lines.append(f"| {k} | {a['tool'][k]} | {b['tool'][k]} |")
    if a["tool"]["spoken_response_in_context"] != b["tool"]["spoken_response_in_context"]:
        notes.append("spoken tool response context entry differs (expected: TTSSpeakFrame.append_to_context default changed in 1.4)")
    roles_a = [m["role"] for m in a["context"]]
    roles_b = [m["role"] for m in b["context"]]
    lines.append("")
    lines.append(f"- context roles {va}: {roles_a}")
    lines.append(f"- context roles {vb}: {roles_b}")
    lines.append("")

    lines.append("## Frame types reaching transport output")
    ta_, tb_ = set(a["output_type_counts"]), set(b["output_type_counts"])
    lines.append(f"- only in {va}: {sorted(ta_ - tb_) or '-'}")
    lines.append(f"- only in {vb}: {sorted(tb_ - ta_) or '-'}")
    lines.append(f"- counts {va}: {a['output_type_counts']}")
    lines.append(f"- counts {vb}: {b['output_type_counts']}")
    lines.append("")
    lines.append(f"<details><summary>Ordered sequence ({va})</summary>\n\n```\n" + "\n".join(a["output_sequence"]) + "\n```\n</details>")
    lines.append(f"<details><summary>Ordered sequence ({vb})</summary>\n\n```\n" + "\n".join(b["output_sequence"]) + "\n```\n</details>")
    lines.append("")

    lines.append("## Turn timing (user turn end decided by)")
    for x, v in ((a, va), (b, vb)):
        lines.append(f"- {v}:")
        for l in x.get("turn_strategy_log", []):
            lines.append(f"  - {l}")
    lines.append("")
    lines.append("## Bot audio bursts (start, end, audio seconds) vs user marks")
    for x, v in ((a, va), (b, vb)):
        finals = [m["t"] for m in x["marks"] if m["event"] == "user_speech_final"]
        starts = [bb["start"] for bb in x.get("bot_audio_bursts", [])]
        gaps = []
        for f in finals:
            nxt = next((s_ for s_ in starts if s_ > f), None)
            gaps.append(round((nxt - f) * 1000) if nxt is not None else None)
        lines.append(f"- {v}: final-transcript -> next bot audio (ms) per user turn: {gaps}")
    lines.append("")
    lines.append("## Teardown")
    for x, v in ((a, va), (b, vb)):
        td = x["teardown"]
        lines.append(f"- {v}: {td['seconds']} s, leftover tasks={td['leftover_tasks'] or 'none'}, warnings during teardown={td['warnings_during_teardown'] or 'none'}")
        if td["leftover_tasks"]:
            notes.append(f"{v} leftover tasks after shutdown: {td['leftover_tasks']}")
    lines.append("")
    lines.append("## pipecat warnings/errors logged")
    for x, v in ((a, va), (b, vb)):
        lines.append(f"- {v}: {len(x['pipecat_warnings_and_errors'])}")
        for m in x["pipecat_warnings_and_errors"][:15]:
            lines.append(f"  - {m}")
    lines.append("")
    lines.append("## Result")
    lines.append("- BLOCKERS: " + ("; ".join(blockers) if blockers else "none"))
    lines.append("- Notes: " + ("; ".join(notes) if notes else "none"))
    print("\n".join(lines))
    return 1 if blockers else 0


def _utterances() -> List[str]:
    return [t for _, t in TURNS] + [BARGE_IN_TEXT]


def _load_speech(speech_dir: str) -> Dict[str, bytes]:
    index = json.load(open(os.path.join(speech_dir, "index.json")))
    missing = [t for t in _utterances() if t not in index]
    if missing:
        raise SystemExit(f"speech dir lacks utterances {missing}; re-run synth")
    return {t: open(os.path.join(speech_dir, f), "rb").read() for t, f in index.items()}


def synth(speech_dir: str, voice: str) -> None:
    """Synthesize the caller's lines with Amazon Polly (8 kHz 16-bit PCM)."""
    import boto3

    polly = boto3.client("polly", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    os.makedirs(speech_dir, exist_ok=True)
    index = {}
    for i, text in enumerate(_utterances()):
        resp = polly.synthesize_speech(
            Text=text, OutputFormat="pcm", SampleRate="8000", VoiceId=voice, Engine="neural"
        )
        name = f"utterance-{i}.pcm"
        with open(os.path.join(speech_dir, name), "wb") as f:
            f.write(resp["AudioStream"].read())
        index[text] = name
    with open(os.path.join(speech_dir, "index.json"), "w") as f:
        json.dump(index, f, indent=2)
    print(json.dumps({t: os.path.getsize(os.path.join(speech_dir, n)) for t, n in index.items()}, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--out", required=True)
    c.add_argument(
        "--speech-dir",
        help="Directory from `synth`: stream real caller speech so pipecat's own VAD/Smart Turn decide turns",
    )
    sy = sub.add_parser("synth")
    sy.add_argument("--speech-dir", required=True)
    sy.add_argument("--voice", default="Matthew")
    d = sub.add_parser("diff")
    d.add_argument("a")
    d.add_argument("b")
    args = ap.parse_args()
    if args.cmd == "capture":
        res = asyncio.run(run_capture(args.out, args.speech_dir))
        print(json.dumps({"checks": res["checks"], "interruption": res["interruption"], "tool": res["tool"], "emf_count_by_kind": res["emf_count_by_kind"]}, indent=2))
        return 0
    if args.cmd == "synth":
        synth(args.speech_dir, args.voice)
        return 0
    return diff(args.a, args.b)


if __name__ == "__main__":
    sys.exit(main())
