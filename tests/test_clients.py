"""Both clients end to end against a fake ingest."""

import asyncio
import logging
import threading

import pytest

import fixwire
from fixwire import AsyncClient, Client


def charge(card):
    raise ValueError("card %s declined for ada@example.com" % card)


def test_sync_client_sends_a_redacted_error_as_an_otlp_log_record(ingest):
    with Client(ingest.dsn, release="web@1.4.0", environment="staging", default_integrations=False) as client:
        fixwire.set_tag("region", "eu")
        try:
            charge("4111 1111 1111 1111")
        except ValueError:
            event_id = client.capture_exception()
        assert client.flush(5)
    [req] = ingest.requests
    assert req["path"] == "/v1/logs"
    assert req["headers"]["Content-Type"] == "application/json" and req["headers"]["Content-Encoding"] == "gzip"
    assert req["headers"]["Authorization"] == "Bearer publickey"
    assert req["headers"]["User-Agent"].startswith("fixwire.python/")
    [(record, resource)] = ingest.records()
    assert resource["telemetry.sdk.name"] == "fixwire.python" and resource["telemetry.sdk.language"] == "python"
    assert resource["service.version"] == "web@1.4.0" and resource["service.name"] == "web"
    assert resource["deployment.environment.name"] == "staging"
    assert record["eventName"] == "exception" and record["severityNumber"] == 17
    assert len(record["traceId"]) == 32 and len(record["spanId"]) == 16
    a = record["attributes"]
    assert a["fixwire.event_id"] == event_id and a["fixwire.handled"] is True
    assert a["exception.type"] == "ValueError"
    # Masked on the device, with the server's rules.
    assert a["exception.message"] == "card [REDACTED:credit_card] declined for [REDACTED:email]"
    [exc] = a["fixwire.exceptions"]
    frame = exc["frames"][-1]  # the raising line last
    assert frame["function"] == "charge" and frame["in_app"] is True and "raise ValueError" in frame["context_line"]
    assert frame["vars"]["card"] == "'[REDACTED:credit_card]'"
    assert set(frame) <= {"function", "module", "file", "abs_path", "line", "in_app", "context_line"} | {
        "pre_context",
        "post_context",
        "vars",
    }
    event = ingest.events()[0]  # as the server reads it
    assert event["event_id"] == event_id and event["sdk"]["name"] == "fixwire.python"
    assert event["release"] == "web@1.4.0" and event["environment"] == "staging" and event["tags"]["region"] == "eu"
    assert event["exception"]["values"][-1]["stacktrace"]["frames"][-1]["function"] == "charge"


def test_before_send_and_sampling(ingest):
    client = Client(
        ingest.dsn, before_send=lambda e, h: None if e.get("message") == "drop me" else e, default_integrations=False
    )
    assert client.capture_message("drop me") is None
    assert client.capture_message("keep me") is not None
    assert client.flush(5)
    client.close()
    assert [e["message"] for e in ingest.events()] == ["keep me"]
    [(record, _)] = ingest.records()
    assert record["eventName"] == "fixwire.message" and record["body"] == {"stringValue": "keep me"}
    assert record["severityNumber"] == 9 and "exception.type" not in record["attributes"]
    assert [r["path"] for r in ingest.requests] == ["/v1/logs"], "no reports of what was dropped"


def test_a_chain_goes_outermost_first_with_everything_the_scopes_know(ingest):
    with Client(ingest.dsn, release="web@1.4.0", default_integrations=False) as client:
        fixwire.set_user({"id": 42, "username": "ada", "ip_address": "10.0.0.1", "plan": "team"})
        fixwire.set_context("order", {"id": "ord_1", "items": 2})
        fixwire.set_extra("attempt", 3)
        fixwire.add_breadcrumb(category="cart", message="checkout started", data={"items": 2})
        with fixwire.new_scope() as scope:
            scope.set_fingerprint(["payments", "{{ default }}"])
            try:
                try:
                    {}["card"]
                except KeyError as e:
                    raise RuntimeError("charge failed") from e
            except RuntimeError as e:
                client.capture_exception(e, mechanism={"type": "worker", "handled": False})
        assert client.flush(5)
    [(record, _)] = ingest.records()
    a = record["attributes"]
    assert record["severityNumber"] == 21  # unhandled: fatal
    assert a["exception.type"] == "RuntimeError" and a["fixwire.handled"] is False
    outer, cause = a["fixwire.exceptions"]
    assert (outer["type"], cause["type"]) == ("RuntimeError", "KeyError")
    assert outer["mechanism"] == {"type": "worker", "handled": False}
    assert a["fixwire.fingerprint"] == ["payments", "{{ default }}"]
    assert a["user.id"] == "42" and a["user.name"] == "ada" and a["client.address"] == "10.0.0.1"
    assert a["user.plan"] == "team"  # other user keys keep their place
    assert a["fixwire.contexts"]["order"] == {"id": "ord_1", "items": 2} and "trace" not in a["fixwire.contexts"]
    assert a["fixwire.contexts"]["runtime"]["name"]
    [crumb] = a["fixwire.breadcrumbs"]
    assert crumb["category"] == "cart" and crumb["data"] == {"items": 2} and isinstance(crumb["timestamp"], float)
    assert a["attempt"] == 3, "extra data: plain attributes, kept by the server as extra"
    event = ingest.events()[0]
    assert [v["type"] for v in event["exception"]["values"]] == ["KeyError", "RuntimeError"]
    assert event["user"] == {"id": "42", "username": "ada", "ip_address": "10.0.0.1"}
    assert event["extra"] == {"attempt": 3, "user.plan": "team"}


def test_a_crash_loop_costs_a_few_events(ingest):
    client = Client(
        ingest.dsn, rate_limit={"per_issue_burst": 5, "per_issue_per_minute": 0.0001}, default_integrations=False
    )
    for _ in range(200):
        try:
            charge("x")
        except ValueError:
            client.capture_exception()
    assert client.flush(5)
    client.close()
    assert len(ingest.events()) == 5


def test_server_errors_are_retried(ingest, monkeypatch):
    import fixwire._core.delivery as delivery

    monkeypatch.setattr(delivery, "BACKOFF_BASE", 0.05)
    ingest.responses = [(503, {}), (503, {})]
    client = Client(ingest.dsn, default_integrations=False)
    client.capture_message("eventually")
    assert ingest.wait(3) == 3
    assert client.flush(5)
    client.close()
    assert [e["message"] for e in ingest.events()] == ["eventually"] * 3


def test_a_rate_limit_pauses_only_the_data_it_names(ingest):
    ingest.responses = [(200, {"Fixwire-Rate-Limits": "60:span"})]
    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False)
    fixwire.capture_message("first")  # answered with a limit on spans
    assert fixwire.flush(5)
    with fixwire.start_span("job"):
        pass
    fixwire.capture_message("second")
    assert fixwire.flush(2) is False, "the spans wait out the limit"
    assert [r["path"] for r in ingest.requests] == ["/v1/logs", "/v1/logs"]


def test_same_exception_object_is_sent_once(ingest):
    client = Client(ingest.dsn, default_integrations=False)
    try:
        charge("x")
    except ValueError as e:
        assert client.capture_exception() is not None
        assert client.capture_exception(e) is None
    client.flush(5)
    client.close()
    assert len(ingest.events()) == 1


def test_async_client_delivers_from_the_loop(ingest):
    captured_from_thread = []

    async def main():
        async with AsyncClient(ingest.dsn, default_integrations=False) as client:
            try:
                charge("x")
            except ValueError:
                client.capture_exception()
            # A capture from another thread reaches the loop too.
            t = threading.Thread(target=lambda: captured_from_thread.append(client.capture_message("from a thread")))
            t.start()
            t.join()
            assert await client.aflush(5)
            # flush() on the loop's own thread would block it: it refuses.
            with pytest.warns(UserWarning):
                assert client.flush() is False

    asyncio.run(main())
    assert captured_from_thread[0] is not None
    messages = sorted(e.get("message") or e["exception"]["values"][-1]["type"] for e in ingest.events())
    assert messages == ["ValueError", "from a thread"]


def test_async_client_hands_over_when_the_loop_stops(ingest):
    holder = {}

    async def main():
        holder["client"] = AsyncClient(ingest.dsn, default_integrations=False)

    asyncio.run(main())  # the loop is now closed
    client = holder["client"]
    client.capture_message("after the loop")
    assert client.flush(5)
    client.close()
    assert [e["message"] for e in ingest.events()] == ["after the loop"]


def test_init_picks_the_transport(ingest):
    assert type(fixwire.init(ingest.dsn, default_integrations=False)) is Client

    async def inside():
        return fixwire.init(ingest.dsn, default_integrations=False)

    client = asyncio.run(inside())
    assert type(client) is AsyncClient
    fixwire.close(1)
    assert type(fixwire.init(ingest.dsn, transport="thread", default_integrations=False)) is Client


def test_logging_integration(ingest):
    fixwire.init(ingest.dsn)
    log = logging.getLogger("shop.checkout")
    log.setLevel(logging.INFO)
    log.info("cart has %d items", 3)
    try:
        charge("x")
    except ValueError:
        log.exception("charge failed for order %s", "ord_1")
    assert fixwire.flush(5)
    event = ingest.events()[0]
    assert event["tags"]["logger"] == "shop.checkout" and event["level"] == "error"
    assert event["message"] == "charge failed for order ord_1"  # the record's body
    assert event["exception"]["values"][-1]["mechanism"]["type"] == "logging"
    assert event["breadcrumbs"]["values"][-1]["message"] == "cart has 3 items"


def test_no_dsn_is_a_noop():
    client = Client(None, default_integrations=False)
    assert not client.enabled and client.capture_message("x") is None and client.flush()


def test_feedback_is_sent_unsampled_with_its_rating_trace_and_event(ingest):
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    with Client(
        ingest.dsn, release="web@2", sample_rate=0.0, traces_sample_rate=1.0, default_integrations=False
    ) as client:
        fixwire.set_user({"id": "u1"})
        rated = client.capture_feedback("  Refunded the wrong order ", score=-1, trace_id=trace_id, source="thumbs")
        crash = client.capture_feedback("It crashed on save", event_id="9ec79c33ec9942ab8353589fcb2e04dc", score=7)
        with fixwire.start_span(name="chat", op="gen_ai.chat") as span:
            client.capture_feedback(score=1)
            current = span.trace_id
        # Nothing to say: nothing is sent.
        assert client.capture_feedback("  ") is None
        assert client.capture_feedback(score=float("nan")) is None
        assert client.flush(5)
    feedback = ingest.bodies("/v1/feedback")
    assert len(feedback) == 3, "feedback isn't sampled"
    first, second, third = feedback
    assert first["feedback_id"] == rated and second["feedback_id"] == crash
    assert {k: first[k] for k in ("message", "source", "score", "trace_id", "release", "environment")} == {
        "message": "Refunded the wrong order",
        "source": "thumbs",
        "score": -1.0,
        "trace_id": trace_id,
        "release": "web@2",
        "environment": "production",
    }
    assert first["sdk"]["name"] == "fixwire.python" and "event_id" not in first
    assert second["event_id"] == "9ec79c33ec9942ab8353589fcb2e04dc"
    assert second["score"] == 1.0, "scores are clamped to [-1, 1]"
    assert third["trace_id"] == current, "the current trace by default"
    assert "message" not in third and third["score"] == 1.0
    assert {r["headers"]["Authorization"] for r in ingest.requests} == {"Bearer publickey"}


def test_feedback_names_who_gave_it_redacted(ingest):
    with Client(ingest.dsn, default_integrations=False) as client:
        fixwire.set_user({"username": "Ada", "email": "ada@example.com"})
        client.capture_feedback("My card 4111 1111 1111 1111 was charged twice", url="https://shop.example/help")
        assert client.flush(5)
    [body] = ingest.bodies("/v1/feedback")
    assert body["name"] == "Ada" and body["email"] == "[REDACTED:email]"
    assert body["message"] == "My card [REDACTED:credit_card] was charged twice"
    assert body["url"] == "https://shop.example/help"


def test_check_ins(ingest):
    with Client(ingest.dsn, release="reports@1", environment="staging", default_integrations=False) as client:
        run = client.capture_check_in(
            "nightly-report",
            "in_progress",
            monitor_config={"schedule": {"type": "crontab", "value": "0 3 * * *"}, "checkin_margin": 5},
        )
        assert run is not None and len(run) == 32
        assert client.capture_check_in("nightly-report", "ok", check_in_id=run, duration=42.5) == run
        with pytest.warns(UserWarning):
            assert client.capture_check_in("nightly-report", "late") is None
        with pytest.warns(UserWarning):
            assert client.capture_check_in("a/b") is None
        assert client.flush(5)
    assert [r["path"] for r in ingest.requests] == ["/v1/check-ins/nightly-report"] * 2
    started, done = ingest.bodies("/v1/check-ins/")
    assert started == {
        "sdk": started["sdk"],
        "check_in_id": run,
        "status": "in_progress",
        "environment": "staging",
        "monitor_config": {"schedule": {"type": "crontab", "value": "0 3 * * *"}, "checkin_margin": 5},
    }
    assert done["check_in_id"] == run and done["status"] == "ok" and done["duration"] == 42.5
    assert "monitor_config" not in done
