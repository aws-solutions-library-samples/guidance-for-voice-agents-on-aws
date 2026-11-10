"""`outbound_ivr.voice.agent._configure_telemetry`: wiring Strands' OpenTelemetry spans to
console/OTLP (local dev, `OUTBOUND_IVR_OTEL`) and/or to the AgentCore Runtime's own
ADOT-configured global tracer provider (`AGENT_OBSERVABILITY_ENABLED`, set by the
platform -- never by this repo).

The gap this guards against: a `StrandsTelemetry()` built with no `tracer_provider`
creates its own, orphaned, never-exported provider, and the old code only ever
constructed one when `OUTBOUND_IVR_OTEL` was set -- so on AgentCore Runtime in production
(`AGENT_OBSERVABILITY_ENABLED=true`, `OUTBOUND_IVR_OTEL` unset), Strands spans were silently
dropped. No real OpenTelemetry export happens here -- `StrandsTelemetry` itself is
faked out.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

pytest.importorskip("strands")

import strands.telemetry
from opentelemetry import trace as otel_trace

from outbound_ivr.voice import agent


class _FakeTelemetry:
    """Records how `StrandsTelemetry(...)` was constructed and which exporter (if
    any) got set up, without touching real OpenTelemetry SDK state."""

    instances: ClassVar[list[_FakeTelemetry]] = []

    def __init__(self, tracer_provider=None):
        self.tracer_provider = tracer_provider
        self.console = False
        self.otlp = False
        _FakeTelemetry.instances.append(self)

    def setup_console_exporter(self):
        self.console = True

    def setup_otlp_exporter(self):
        self.otlp = True


@pytest.fixture(autouse=True)
def _fake_strands_telemetry(monkeypatch):
    _FakeTelemetry.instances = []
    monkeypatch.setattr(strands.telemetry, "StrandsTelemetry", _FakeTelemetry)
    yield


@pytest.fixture(autouse=True)
def _clear_telemetry_env(monkeypatch):
    # Both triggers are opt-in; make sure no test leaks the other's env var in.
    monkeypatch.delenv("OUTBOUND_IVR_OTEL", raising=False)
    monkeypatch.delenv("AGENT_OBSERVABILITY_ENABLED", raising=False)


def test_does_nothing_when_neither_trigger_is_set():
    agent._configure_telemetry()

    assert _FakeTelemetry.instances == []


@pytest.mark.parametrize("off_value", ["0", "false", "False", "no", ""])
def test_agent_observability_enabled_falsy_values_do_nothing(monkeypatch, off_value):
    monkeypatch.setenv("AGENT_OBSERVABILITY_ENABLED", off_value)

    agent._configure_telemetry()

    assert _FakeTelemetry.instances == []


def test_outbound_ivr_otel_console_sets_up_the_console_exporter_with_no_tracer_provider(
    monkeypatch,
):
    monkeypatch.setenv("OUTBOUND_IVR_OTEL", "console")

    agent._configure_telemetry()

    [telemetry] = _FakeTelemetry.instances
    assert telemetry.tracer_provider is None
    assert telemetry.console is True
    assert telemetry.otlp is False


def test_outbound_ivr_otel_otlp_sets_up_the_otlp_exporter(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_OTEL", "otlp")

    agent._configure_telemetry()

    [telemetry] = _FakeTelemetry.instances
    assert telemetry.otlp is True
    assert telemetry.console is False


def test_outbound_ivr_otel_rejects_an_unknown_value(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_OTEL", "bogus")

    with pytest.raises(SystemExit):
        agent._configure_telemetry()


def test_agent_observability_enabled_wires_the_global_tracer_provider(monkeypatch):
    # This is the regression this change fixes: on AgentCore Runtime,
    # AGENT_OBSERVABILITY_ENABLED is set but OUTBOUND_IVR_OTEL is not -- Strands must still
    # get a non-None tracer_provider (the ADOT-configured global one), and must not
    # get a redundant console/OTLP exporter set up on top of it.
    monkeypatch.setenv("AGENT_OBSERVABILITY_ENABLED", "true")

    agent._configure_telemetry()

    [telemetry] = _FakeTelemetry.instances
    assert telemetry.tracer_provider is otel_trace.get_tracer_provider()
    assert telemetry.console is False
    assert telemetry.otlp is False


def test_both_triggers_set_combines_tracer_provider_and_console_exporter(monkeypatch):
    monkeypatch.setenv("AGENT_OBSERVABILITY_ENABLED", "1")
    monkeypatch.setenv("OUTBOUND_IVR_OTEL", "console")

    agent._configure_telemetry()

    [telemetry] = _FakeTelemetry.instances
    assert telemetry.tracer_provider is otel_trace.get_tracer_provider()
    assert telemetry.console is True
