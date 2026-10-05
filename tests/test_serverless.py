"""Serverless handlers: each invocation is its own scope and segment,
exceptions are reported, and events are sent before the handler returns."""

import asyncio
import time

import pytest

import fixwire


class LambdaContext:
    function_name = "charge-card"
    function_version = "$LATEST"
    aws_request_id = "req-1"
    invoked_function_arn = "arn:aws:lambda:eu-central-1:123:function:charge-card"

    def __init__(self, left_ms=30_000):
        self.left_ms = left_ms

    def get_remaining_time_in_millis(self):
        return self.left_ms


def charge(card):
    raise ValueError(f"card {card} declined")


def test_a_sync_handler(ingest):
    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False)

    @fixwire.serverless_function
    def handler(event, context):
        fixwire.set_tag("card", event["card"])
        if event["card"] == "4000":
            charge(event["card"])
        return {"ok": True}

    started = time.monotonic()
    assert handler({"card": "1234"}, LambdaContext()) == {"ok": True}
    with pytest.raises(ValueError):
        handler({"card": "4000"}, LambdaContext())
    assert len(ingest.events()) == 1  # sent before the handler raised
    assert time.monotonic() - started < 1.5
    [event] = ingest.events()
    assert event["contexts"]["aws_lambda"]["function_name"] == "charge-card"
    assert event["contexts"]["aws_lambda"]["aws_request_id"] == "req-1"
    assert event["tags"]["serverless.function"] == "charge-card"
    assert event["tags"]["card"] == "4000"  # its own scope: not the first call's tag
    segments = [s for s in ingest.spans() if s.get("is_segment")]
    assert {s["name"] for s in segments} == {"charge-card"} and len(segments) == 2
    assert {s["attributes"]["fixwire.op"] for s in segments} == {"function.aws.lambda"}
    assert {s["kind"] for s in segments} == {2}  # server


def test_an_invocation_continues_the_callers_trace(ingest):
    trace, parent = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    fixwire.init(ingest.dsn, traces_sample_rate=0.0, default_integrations=False)

    @fixwire.serverless_function
    def handler(event, context):
        fixwire.capture_message("charged")
        return fixwire.trace_headers()

    # An API Gateway event: the request's headers, as the caller sent them.
    headers = handler({"headers": {"Traceparent": "00-%s-%s-01" % (trace, parent), "baggage": "tenant=acme"}}, None)
    assert headers["traceparent"].startswith("00-%s-" % trace) and headers["baggage"] == "tenant=acme"
    [segment] = ingest.spans()  # sampled by the caller, although our rate is 0
    assert segment["trace_id"] == trace and segment["parent_span_id"] == parent and segment["is_segment"]
    assert ingest.events()[0]["contexts"]["trace"]["trace_id"] == trace


def test_an_async_handler_with_options(ingest):
    async def main():
        fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False, transport="thread")

        @fixwire.serverless_function(name="nightly-report", flush_timeout=1)
        async def handler(event):
            await asyncio.sleep(0)
            raise RuntimeError("report failed")

        with pytest.raises(RuntimeError):
            await handler({})

    asyncio.run(main())
    assert len(ingest.events()) == 1
    [event] = ingest.events()
    assert event["tags"]["serverless.function"] == "nightly-report"
    assert "aws_lambda" not in event.get("contexts", {})


def test_no_time_left_means_no_wait(ingest):
    fixwire.init(ingest.dsn, default_integrations=False)

    @fixwire.serverless_function(flush_timeout=5)
    def handler(event, context):
        fixwire.capture_message("almost out of time")

    started = time.monotonic()
    handler({}, LambdaContext(left_ms=200))  # under the half-second margin
    assert time.monotonic() - started < 0.5
    assert fixwire.flush(5)
    assert len(ingest.events()) == 1
