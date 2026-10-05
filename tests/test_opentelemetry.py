"""The app's own OpenTelemetry: linked, never replaced."""

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

import fixwire
from fixwire.integrations.opentelemetry import OpenTelemetryIntegration, otlp_exporter_options

TRACE_ID = 0x4BF92F3577B34DA6A3CE929D0E0E4736
SPAN_ID = 0x00F067AA0BA902B7


def otel_span():
    return NonRecordingSpan(
        SpanContext(trace_id=TRACE_ID, span_id=SPAN_ID, is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED))
    )


def test_errors_in_otel_spans_carry_their_trace_and_fixwire_spans_win(ingest):
    fixwire.init(
        ingest.dsn, traces_sample_rate=1.0, default_integrations=False, integrations=[OpenTelemetryIntegration()]
    )
    with trace.use_span(otel_span()):
        fixwire.capture_message("inside an OTel span")
        with fixwire.start_span("job", op="task") as span:
            fixwire.capture_message("inside a Fixwire span")
            fixwire_trace = span.trace_id
    fixwire.capture_message("outside both")
    assert fixwire.flush(5)
    traces = {e["message"]: e["contexts"]["trace"] for e in ingest.events() if "message" in e}
    assert traces["inside an OTel span"] == {
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": "00f067aa0ba902b7",
    }
    assert traces["inside a Fixwire span"]["trace_id"] == fixwire_trace
    assert traces["outside both"]["trace_id"] != "4bf92f3577b34da6a3ce929d0e0e4736"


def test_otlp_exporter_options():
    o = otlp_exporter_options("https://fw_pk_live_abc@ingest.eu.fixwire.io")
    assert o["traces"] == {
        "endpoint": "https://ingest.eu.fixwire.io/v1/traces",
        "headers": {"Authorization": "Bearer fw_pk_live_abc"},
    }
    assert o["logs"]["endpoint"] == "https://ingest.eu.fixwire.io/v1/logs"
    assert (
        otlp_exporter_options("http://k@localhost:8082/fixwire")["traces"]["endpoint"]
        == "http://localhost:8082/fixwire/v1/traces"
    )
