"""The sans-IO core: DSNs, the event builder, scopes, budgets, delivery."""

import asyncio
import sys
import threading

import pytest

from fixwire._core import event_builder, scope
from fixwire._core.delivery import MAX_ATTEMPTS, Delivery, Outbound, parse_rate_limits
from fixwire._core.dsn import BadDsn, Dsn
from fixwire._core.limiter import Limiter, fingerprint, template


def test_dsn():
    d = Dsn.parse("https://fw_pk_live_abc@ingest.eu.fixwire.io")
    assert d.base_url == "https://ingest.eu.fixwire.io" and d.key == "fw_pk_live_abc"
    assert d.url("/v1/logs") == "https://ingest.eu.fixwire.io/v1/logs"
    assert d.auth_header() == "Bearer fw_pk_live_abc"
    assert str(d) == "https://fw_pk_live_abc@ingest.eu.fixwire.io"
    # A self-hosted server behind a prefix.
    d = Dsn.parse("http://k@fixwire.internal:8443/ingest/")
    assert d.url("/v1/traces") == "http://fixwire.internal:8443/ingest/v1/traces"
    assert str(d) == "http://k@fixwire.internal:8443/ingest"
    for bad in ("ftp://a@b", "https://b", "https://a@", "https://a@b:port"):
        with pytest.raises(BadDsn):
            Dsn.parse(bad)


def test_no_dsn_fallbacks_but_fixwire_dsn(monkeypatch):
    from fixwire._core.options import Options

    monkeypatch.setenv("FIXWIRE_DSN", "https://k@ingest.example")
    monkeypatch.setenv("FIXWIRE_RELEASE", "api@2")
    o = Options()
    assert o.dsn == "https://k@ingest.example" and o.release == "api@2" and o.environment == "production"
    monkeypatch.delenv("FIXWIRE_DSN")
    monkeypatch.delenv("FIXWIRE_RELEASE")
    for name in ("DSN", "RELEASE", "ENVIRONMENT"):
        monkeypatch.setenv("OTHER_" + name, "ignored")
    o = Options()
    assert o.dsn is None and o.release is None and o.environment == "production"


def _raise_chain():
    try:
        {}["missing"]
    except KeyError as e:
        secret_local = "x" * 5000  # noqa: F841 - bounded in vars
        raise ValueError("charge failed") from e


def test_exception_chain_and_in_app(tmp_path):
    o = event_builder.Options(project_root=__file__.rsplit("/", 1)[0], max_value_length=100)
    try:
        _raise_chain()
    except ValueError as e:
        values = event_builder.exceptions_from_error_tuple((type(e), e, e.__traceback__), o)
    assert [v["type"] for v in values] == ["KeyError", "ValueError"]
    frames = values[-1]["stacktrace"]["frames"]
    top = frames[-1]
    assert top["function"] == "_raise_chain" and top["in_app"] is True and top["module"] == "test_core"
    # Locals are bounded by max_value_length and captured for in-app frames only.
    assert len(top["vars"]["secret_local"]) <= 100
    assert "context_line" not in top  # added later, off the caller's thread
    event = {"exception": {"values": values}}
    event_builder.add_source_context(event, 200)
    assert "raise ValueError" in top["context_line"]


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup is new in 3.11")
def test_exception_group():
    try:
        raise ExceptionGroup("batch", [ValueError("a"), TypeError("b")])  # noqa: F821 (3.11+ only)
    except ExceptionGroup as e:  # noqa: F821
        values = event_builder.exceptions_from_error_tuple((type(e), e, e.__traceback__), event_builder.Options())
    types = sorted(v["type"] for v in values)
    assert types == ["ExceptionGroup", "TypeError", "ValueError"]
    group = [v for v in values if v["type"] == "ExceptionGroup"][0]
    assert group["mechanism"]["is_exception_group"] and group["mechanism"]["exception_id"] == 0
    children = [v for v in values if v["type"] != "ExceptionGroup"]
    assert all(
        c["mechanism"]["parent_id"] == 0 and c["mechanism"]["source"].startswith("exceptions[") for c in children
    )


def test_newest_frames_are_kept():
    def recurse(n):
        if n == 0:
            raise RuntimeError("deep")
        recurse(n - 1)

    o = event_builder.Options(max_stack_frames=10)
    try:
        recurse(50)
    except RuntimeError as e:
        values = event_builder.exceptions_from_error_tuple((type(e), e, e.__traceback__), o)
    frames = values[0]["stacktrace"]["frames"]
    assert len(frames) == 10 and frames[-1]["function"] == "recurse"


def test_scopes_isolate_threads_and_tasks():
    scope.get_isolation_scope().set_tag("app", "web")
    seen = {}

    def worker(name):
        with scope.isolation_scope() as s:
            s.set_tag("request", name)
            event = {}
            scope.apply(event, 100)
            seen[name] = event["tags"]

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"a": {"app": "web", "request": "a"}, "b": {"app": "web", "request": "b"}}

    async def task(name):
        with scope.isolation_scope() as s:
            s.set_tag("task", name)
            await asyncio.sleep(0)
            event = {}
            scope.apply(event, 100)
            return event["tags"]["task"]

    async def main():
        return await asyncio.gather(task("x"), task("y"))

    assert asyncio.run(main()) == ["x", "y"]
    event = {}
    scope.apply(event, 100)
    assert event["tags"] == {"app": "web"}


def test_scope_precedence():
    scope.get_global_scope().set_tag("level", "global")
    scope.get_isolation_scope().set_tag("level", "isolation")
    with scope.new_scope() as s:
        s.set_tag("level", "current")
        s.set_user({"id": "u1"})
        event = {"tags": {"own": "event"}}
        scope.apply(event, 100)
    assert event["tags"] == {"level": "current", "own": "event"} and event["user"] == {"id": "u1"}


def test_budgets_count_what_they_suppress():
    lim = Limiter(per_issue_burst=3, per_issue_per_minute=1, global_per_minute=600)
    results = [lim.allow("fp", 1000.0 + i * 0.01) for i in range(10)]
    assert [ok for ok, _ in results] == [True] * 3 + [False] * 7
    # A minute later one more goes, carrying the count.
    ok, suppressed = lim.allow("fp", 1061.0)
    assert ok and suppressed["count"] == 7 and suppressed["first"] == pytest.approx(1000.03)
    # Other issues are unaffected.
    assert lim.allow("other", 1061.0)[0]


def test_fingerprint_ignores_lines_and_numbers():
    def ev(line, value):
        return {
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "value": value,
                        "stacktrace": {
                            "frames": [{"module": "app.checkout", "function": "charge", "lineno": line, "in_app": True}]
                        },
                    }
                ]
            }
        }

    assert fingerprint(ev(10, "amount 500")) == fingerprint(ev(42, "amount 700"))
    assert fingerprint({"message": "order 123 failed for a@b.io"}) == fingerprint(
        {"message": "order 9 failed for c@d.io"}
    )
    assert template("id 0xdeadbeef, 9ec79c33-ec99-42ab-8353-589fcb2e04dc") == "id <*>, <*>"


def req(body, category="error"):
    return Outbound("/v1/logs", "application/json", body, category)


def test_delivery_retries():
    d = Delivery(rng=lambda: 0.0)
    d.offer(req(b"x"), 0.0)
    sent = d.next(0.0)
    assert d.on_error(sent, 0.0).retry and d.next(0.1) is None
    assert d.wake_at() == pytest.approx(0.5)  # 1 s backoff, jitter halves at worst
    for attempt in range(1, MAX_ATTEMPTS):
        now = 1000.0 * attempt
        sent = d.next(now)
        dec = d.on_response(sent, 503, {}, now)
    assert dec.dropped and d.empty()

    # 5xx wait at least Retry-After.
    d.offer(req(b"busy"), 0.0)
    assert d.on_response(d.next(0.0), 503, {"retry-after": "30"}, 0.0).retry
    assert d.next(29.0) is None and d.next(30.0).body == b"busy"
    d.offer(req(b"broken"), 0.0)
    assert d.on_response(d.next(0.0), 500, {}, 0.0).retry and d.next(100.0).body == b"broken"

    # Other 4xx are final.
    for status in (400, 401, 403, 404, 413, 415):
        d.offer(req(b"y"), 0.0)
        assert d.on_response(d.next(0.0), status, {}, 0.0).dropped and d.empty()


def test_rate_limits_pause_some_data_while_the_rest_flows():
    d = Delivery(rng=lambda: 0.0)
    d.offer(req(b"first"), 0.0)
    d.on_response(d.next(0.0), 200, {"fixwire-rate-limits": "60:log;span, 3600:file"}, 0.0)
    assert d.limited("log", 59.0) and d.limited("span", 59.0) and not d.limited("log", 60.0)
    assert d.limited("file", 3599.0) and not d.limited("error", 1.0)
    # Paused data waits in the queue; errors keep flowing.
    d.offer(req(b"spans", "span"), 1.0)
    d.offer(req(b"error"), 1.0)
    assert d.next(1.0).body == b"error" and d.next(1.0) is None
    assert d.wake_at() == 60.0 and d.next(60.0).body == b"spans"

    # A 429 waits Retry-After, then the request is tried again.
    d.offer(req(b"limited", "session"), 100.0)
    item = d.next(100.0)
    dec = d.on_response(item, 429, {"retry-after": "60", "fixwire-rate-limits": "60:session"}, 100.0)
    assert dec.retry and d.next(159.0) is None and d.next(160.0) is item
    # A 429 that names no data pauses all of it.
    d.offer(req(b"busy"), 200.0)
    d.on_response(d.next(200.0), 429, {"retry-after": "2"}, 200.0)
    assert d.limited("span", 201.0) and not d.limited("span", 202.0)
    # So does an empty category list (the server's "busy").
    d.on_response(req(b"x"), 200, {"fixwire-rate-limits": "5:"}, 300.0)
    assert d.limited("check_in", 304.0)


def test_rate_limit_header():
    limits = parse_rate_limits("60:log;span, 3600:file, 10:, bad, x:error", 100.0)
    assert limits == {"log": 160.0, "span": 160.0, "file": 3700.0, "": 110.0}


def test_queue_overflow_drops_paused_data_first_then_the_oldest():
    d = Delivery(max_items=2)
    for b in (b"1", b"2", b"3"):
        d.offer(req(b), 0.0)
    assert [i.body for i in d.queue] == [b"2", b"3"] and d.overflowed == 1
    # Spans are paused: when the queue is full, they go before any error.
    d.max_items = 3
    d.limits["span"] = 100.0
    d.offer(req(b"spans", "span"), 0.0)
    d.offer(req(b"4"), 0.0)
    assert [i.body for i in d.queue] == [b"2", b"3", b"4"] and d.overflowed == 2
