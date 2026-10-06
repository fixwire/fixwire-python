"""Both clients end to end against a fake ingest."""

import asyncio
import logging
import os
import signal
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fixwire
from fixwire import AsyncClient, Client
from fixwire.transport.httpx_ import HttpxSender
from fixwire.transport.urllib3_ import Urllib3Sender


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


def test_logging_while_reporting_is_not_reported_again(ingest):
    hooks = logging.getLogger("shop.hooks")

    def before_send(event, hint):
        hooks.error("before_send saw %s", event.get("logentry", {}).get("formatted"))
        return event

    fixwire.init(ingest.dsn, before_send=before_send)
    logging.getLogger("shop").error("charge failed")
    assert fixwire.flush(5)
    assert [e["message"] for e in ingest.events()] == ["charge failed"], "no recursion through before_send"


class Unreadable(Exception):
    @property
    def message(self):
        raise KeyError("message")


def test_an_exception_that_breaks_when_read_is_not_raised_into_the_app(ingest):
    with Client(ingest.dsn, default_integrations=False) as client:
        try:
            raise Unreadable("x")
        except Unreadable:
            assert client.capture_exception() is None


BEGIN = "-----BEGIN "  # split, so no scanner sees a whole key
KEY = BEGIN + "RSA PRIVATE KEY-----\n" + "MIIEowIBAAKCAQEA" * 120 + "\n-----END RSA PRIVATE KEY-----"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJ" + "c3ViIjoiYWRhIn0" * 200 + ".c2lnbmF0dXJlLXNpZ25hdHVyZQ"


def _hold(secret):
    held = "q" * 900 + " " + secret  # noqa: F841 - a local the cut goes through
    raise ValueError("held")


def test_strings_are_redacted_then_cut_in_bytes(ingest):
    with Client(ingest.dsn, default_integrations=False) as client:
        fixwire.set_extra("fits", "é" * 512)  # 1,024 bytes
        fixwire.set_extra("over", "é" * 512 + "a")  # 1,025 bytes
        # Secrets the cut at 1,024 bytes goes through: masked whole.
        fixwire.set_extra("key", "x" * 100 + KEY)
        fixwire.set_extra("jwt", "y" * 600 + " " + JWT)
        try:
            _hold(JWT)
        except ValueError:
            client.capture_exception()
        assert client.flush(5)
    [(record, _)] = ingest.records()
    a = record["attributes"]
    assert a["fits"] == "é" * 512
    assert a["over"] == "é" * 510 + "..." and len(a["over"].encode()) == 1023
    assert a["key"] == "x" * 100 + "[REDACTED:private_key]"
    assert a["jwt"] == "y" * 600 + " [REDACTED:jwt]"
    held = a["fixwire.exceptions"][0]["frames"][-1]["vars"]["held"]
    assert held == "'" + "q" * 900 + " [REDACTED:jwt]'"
    body = ingest.requests[0]["body"].decode()  # the source lines show how KEY and JWT are made, once
    assert "MIIEowIBAAKCAQEA" * 2 not in body and "c3ViIjoiYWRhIn0" * 2 not in body
    # Nothing on the wire is longer than 1,024 bytes (the exception's frames included).
    for frame in a["fixwire.exceptions"][0]["frames"]:
        for v in frame.get("vars", {}).values():
            assert len(v.encode()) <= 1024


def test_a_query_is_redacted_as_part_of_its_url(ingest):
    with Client(ingest.dsn, default_integrations=False) as client:
        client.capture_event(
            {
                "message": "callback failed",
                "request": {
                    "method": "GET",
                    "url": "https://shop.example/cb",
                    "query_string": "code=SplxlOBeZQQYbYS6&state=x1",
                },
            }
        )
        assert client.flush(5)
    [(record, _)] = ingest.records()
    assert record["attributes"]["url.full"] == "https://shop.example/cb?code=[REDACTED:secret_assignment]&state=x1"


def test_a_string_redaction_fails_on_is_sent_filtered(ingest, monkeypatch):
    from fixwire._core.redact import Redactor

    real = Redactor.mask

    def mask(self, s):
        if "boom" in s:
            raise RuntimeError("redaction failed")
        return real(self, s)

    monkeypatch.setattr(Redactor, "mask", mask)
    with Client(ingest.dsn, default_integrations=False) as client:
        fixwire.set_extra("note", "boom: ada@example.com")
        client.capture_message("boom for ada@example.com")
        assert client.flush(5)
    [(record, _)] = ingest.records()
    assert record["body"]["stringValue"] == "[Filtered]" and record["attributes"]["note"] == "[Filtered]"
    assert "ada@example.com" not in ingest.requests[0]["body"].decode()


def test_an_event_over_1_mb_sheds_breadcrumbs_then_vars_then_contexts(monkeypatch):
    import gzip
    import json

    from fixwire._core import pipeline
    from fixwire._core.options import Options

    monkeypatch.setattr(pipeline, "MAX_EVENT_BYTES", 40_000)
    core = pipeline.Core(Options(dsn="https://k@ingest.example", include_source_context=False))

    def sent(crumbs, local_vars, contexts, extra=0):
        event = {
            "event_id": "0" * 32,
            "level": "error",
            "breadcrumbs": {"values": [{"message": "c%d" % i + "c" * 1000} for i in range(crumbs)]},
            "exception": {
                "values": [
                    {
                        "type": "E",
                        "value": "v",
                        "stacktrace": {
                            "frames": [{"function": "f", "vars": {"v%d" % i: "x" * 1000 for i in range(local_vars)}}]
                        },
                    }
                ]
            },
            "contexts": {
                "trace": {"trace_id": "a" * 32, "span_id": "b" * 16},
                "order": {"k%d" % i: "y" * 1000 for i in range(contexts)},
            },
            "extra": {"e%d" % i: "z" * 1000 for i in range(extra)},
        }
        out = core.encode(event)
        if not out:
            return None
        [record] = json.loads(gzip.decompress(out[0].body))["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        keys = {a["key"] for a in record["attributes"]}
        return {
            "breadcrumbs": "fixwire.breadcrumbs" in keys,
            "vars": b'"vars"' in gzip.decompress(out[0].body),
            "contexts": "fixwire.contexts" in keys,
            "trace": "traceId" in record,
        }

    everything = {"breadcrumbs": True, "vars": True, "contexts": True, "trace": True}
    assert sent(10, 10, 10) == everything
    assert sent(40, 10, 10) == {**everything, "breadcrumbs": False}
    assert sent(10, 40, 10) == {**everything, "breadcrumbs": False, "vars": False}
    assert sent(10, 10, 40) == {"breadcrumbs": False, "vars": False, "contexts": False, "trace": True}
    assert sent(10, 10, 10, extra=50) is None, "still over: dropped"


def test_async_client_flush_and_close_keep_to_their_timeout(ingest):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def make():
        return AsyncClient(ingest.dsn, default_integrations=False)

    client = asyncio.run_coroutine_threadsafe(make(), loop).result(5)
    busy = threading.Event()
    loop.call_soon_threadsafe(busy.wait, 5)  # the loop is stuck in the app's code
    client.capture_message("while the loop is busy")
    started = time.monotonic()
    assert client.flush(0.5) is False
    client.close(0.5)
    assert time.monotonic() - started < 1.5
    busy.set()
    asyncio.run_coroutine_threadsafe(client._async_driver.aclose(5), loop).result(10)  # the rest, once free
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork()")
def test_a_forked_child_does_not_wait_on_locks_held_at_the_fork(ingest):
    client = Client(ingest.dsn, default_integrations=False)
    client.capture_message("parent")
    assert client.flush(5)
    # As if other threads were inside the queue and the budgets at the fork.
    with client.core.queue._lock, client.core.limiter._lock, warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork() while threads run
        pid = os.fork()
        if pid == 0:
            try:
                client.capture_message("child")
                os._exit(0 if client.flush(5) else 1)
            finally:
                os._exit(2)
    deadline = time.monotonic() + 10
    while not os.waitpid(pid, os.WNOHANG)[0] and time.monotonic() < deadline:
        time.sleep(0.05)
    if time.monotonic() >= deadline:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        pytest.fail("the child hung on a lock held at the fork")
    client.close()
    assert sorted(e["message"] for e in ingest.events()) == ["child", "parent"]


def test_answers_are_read_bounded_and_redirects_are_not_followed():
    # A redirect would take the key to another host; a huge answer was read whole.
    elsewhere: list[str | None] = []

    class Other(BaseHTTPRequestHandler):
        def do_POST(self):
            elsewhere.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    class Hostile(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/v1/moved":
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:%d/v1/logs" % other.server_address[1])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(1 << 30))
            self.end_headers()
            try:
                for _ in range(1 << 10):
                    self.wfile.write(b"x" * (1 << 20))
            except OSError:
                pass  # the SDK stopped reading

        def log_message(self, *args):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (Other, Hostile)]
    other, hostile = servers
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % hostile.server_address[1]
    headers = {"Authorization": "Bearer publickey"}

    async def via_httpx():
        send = HttpxSender(5)
        try:
            return (await send(base + "/v1/moved", b"{}", headers))[0], (await send(base + "/v1/logs", b"{}", headers))[
                0
            ]
        finally:
            await send.aclose()

    try:
        send = Urllib3Sender(5)
        started = time.perf_counter()
        assert send(base + "/v1/moved", b"{}", headers)[0] == 307
        assert send(base + "/v1/logs", b"{}", headers)[0] == 200
        assert asyncio.run(via_httpx()) == (307, 200)
        assert time.perf_counter() - started < 5, "a 1 GB answer isn't read"
        assert elsewhere == []
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


def test_no_dsn_is_a_noop():
    client = Client(None, default_integrations=False)
    assert not client.enabled and client.capture_message("x") is None and client.flush()


@pytest.mark.parametrize(
    ("dsn", "options", "said"),
    [
        ("ftp://k@ingest.example", {}, "scheme is 'ftp'"),
        ("https://ingest.example", {}, "no key"),
        ("https://k@", {}, "no host"),
        ("https://k@ingest.example:port", {}, "bad port"),
        ("https://k@ingest.example", {"dns": "https://k@typo.example"}, "unknown option(s): dns"),
        ("https://k@ingest.example", {"sample_rate": 1.5}, "sample_rate"),
        ("https://k@ingest.example", {"max_breadcrumbs": -1}, "max_breadcrumbs"),
        ("https://k@ingest.example", {"rate_limit": {"burst": 1}}, "burst"),
        ("https://k@ingest.example", {"transport": "asyncio"}, "needs a running event loop"),
    ],
)
def test_init_never_raises_a_broken_dsn_or_option_is_said_and_the_sdk_stays_off(dsn, options, said, monkeypatch):
    monkeypatch.setenv("FIXWIRE_DSN", "https://k@fallback.example")  # not read instead
    with pytest.warns(UserWarning, match="^fixwire is off: .*" + said.replace("(", r"\(").replace(")", r"\)")) as w:
        client = fixwire.init(dsn, **options)
    assert w[0].filename == __file__, "the warning points at the app's line"
    assert not client.enabled and not client.options.default_integrations
    assert fixwire.capture_message("x") is None and fixwire.flush(1)
    if "transport" not in options:  # the clients themselves don't raise either
        with pytest.warns(UserWarning, match="^fixwire is off: ") as w:
            assert not Client(dsn, **options).enabled
        assert w[0].filename == __file__


def test_a_broken_dsn_is_said_on_stderr_without_debug():
    import subprocess
    import sys

    script = "import fixwire\nclient = fixwire.init('https://ingest.example')\nprint(client.enabled)\n"
    env = {**os.environ, "PYTHONWARNINGS": ""}
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0 and out.stdout == "False\n"
    assert "<string>:2: UserWarning: fixwire is off: the DSN has no key" in out.stderr


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


def test_feedback_is_redacted_then_cut(ingest):
    with Client(ingest.dsn, max_value_length=40, default_integrations=False) as client:
        fixwire.set_user({"username": "Ada " * 20, "email": "ada@example.com"})
        client.capture_feedback("Refund ada@example.com " + "x" * 100, url="https://shop.example/a?token=" + "s" * 40)
        client.capture_feedback("y" * 40)
        assert client.flush(5)
    cut, fits = ingest.bodies("/v1/feedback")
    assert cut["message"] == "Refund [REDACTED:email] " + "x" * 13 + "...", "redacted, then cut to 40 bytes"
    assert cut["name"] == ("Ada " * 10)[:37] + "..." and cut["email"] == "[REDACTED:email]"
    assert cut["url"] == "https://shop.example/a?token=[REDACTE..."
    assert fits["message"] == "y" * 40


def test_the_apps_configuration_is_cut_but_never_redacted(ingest, monkeypatch):
    # Each an email to a detector: masked, release health would break.
    monkeypatch.setenv("OTEL_SERVICE_NAME", "checkout@team.example" + "s" * 40)
    host = "web@pod.example" + "h" * 40
    client = fixwire.init(
        ingest.dsn,
        release="api@1.2.3.example",
        environment="qa@ci.example",
        server_name=host,
        max_value_length=40,
        traces_sample_rate=1.0,
        default_integrations=False,
    )
    fixwire.capture_message("deployed by ada@example.com")
    with fixwire.start_span("checkout"):
        pass
    client.start_request_session()()
    monitor = "ops@cron.example-" + "r" * 60
    client.capture_check_in(monitor, monitor_config={"owner": "ops@team.example"})
    client.capture_feedback("thanks")
    assert fixwire.flush(5)
    [(record, res)] = ingest.records()
    assert record["body"]["stringValue"] == "deployed by [REDACTED:email]", "the app's data still is"
    for r in [res] + [r for _, r in ingest.otlp_spans()]:
        assert r["service.name"] == "checkout@team.example" + "s" * 16 + "..."
        assert r["service.version"] == "api@1.2.3.example" and r["deployment.environment.name"] == "qa@ci.example"
        assert r["host.name"] == host[:37] + "..."
    [sessions] = ingest.bodies("/v1/sessions")
    [feedback] = ingest.bodies("/v1/feedback")
    for body in (sessions, feedback):
        assert (body["release"], body["environment"]) == ("api@1.2.3.example", "qa@ci.example")
    [check_in] = [r for r in ingest.requests if r["path"].startswith("/v1/check-ins/")]
    assert check_in["path"] == "/v1/check-ins/ops%40cron.example-" + "r" * 20 + "..."
    assert check_in["json"]["monitor_config"] == {"owner": "ops@team.example"}
    assert check_in["json"]["environment"] == "qa@ci.example"


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
