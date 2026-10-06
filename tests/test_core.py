"""The sans-IO core: DSNs, the event builder, scopes, budgets, delivery."""

import asyncio
import collections
import email.utils
import os
import sys
import threading
import time

import pytest

from fixwire._core import event_builder, scope
from fixwire._core.delivery import MAX_ATTEMPTS, MAX_WAIT, Delivery, Outbound, parse_rate_limits
from fixwire._core.dsn import BadDsn, Dsn
from fixwire._core.limiter import Limiter, fingerprint, template
from fixwire._core.serializer import CIRCULAR, Serializer, clip


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
    # Locals are captured for in-app frames only, kept to what redaction
    # reads (cut to max_value_length once redacted, on the delivery side).
    assert top["vars"]["secret_local"] == repr("x" * 5000)
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

    # The default keeps 100 of 101: the oldest call (this test) goes.
    def deep(n):
        if n == 0:
            raise RuntimeError("deep")
        deep(n - 1)

    try:
        deep(99)  # this test's frame and 100 of deep()'s
    except RuntimeError as e:
        values = event_builder.exceptions_from_error_tuple((type(e), e, e.__traceback__), event_builder.Options())
    frames = values[0]["stacktrace"]["frames"]
    assert len(frames) == 100 and {f["function"] for f in frames} == {"deep"}


def _chain(n):
    """An exception with n - 1 causes."""
    error = None
    for i in range(n):
        try:
            raise ValueError("error %d" % i) from error
        except ValueError as e:
            error = e
    return error


def test_a_chain_keeps_ten_exceptions_and_stops_where_it_comes_back():
    e = _chain(11)
    values = event_builder.exceptions_from_error_tuple((type(e), e, e.__traceback__), event_builder.Options())
    # Causes first: the one raised (10) last, its cause (0) cut.
    assert [v["value"] for v in values] == ["error %d" % i for i in range(1, 11)]
    a, b = ValueError("a"), ValueError("b")
    a.__context__, b.__context__ = b, a  # a loop
    values = event_builder.exceptions_from_error_tuple((ValueError, a, None), event_builder.Options())
    assert [v["value"] for v in values] == ["b", "a"]


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup is new in 3.11")
def test_a_group_keeps_ten_exceptions():
    group = ExceptionGroup("batch", [ValueError(str(i)) for i in range(20)])  # noqa: F821 (3.11+ only)
    values = event_builder.exceptions_from_error_tuple((type(group), group, None), event_builder.Options())
    assert len(values) == 10 and values[-1]["type"] == "ExceptionGroup"


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


@pytest.mark.parametrize(
    "message",
    ["@" * 100_000, "a" * 50_000 + "@" + "a" * 50_000, "a@" * 50_000, "x " * 50_000],
    ids=["at signs", "one at sign", "at pairs", "words"],
)
def test_fingerprints_of_hostile_messages_are_cheap(message):
    # On the caller's thread: "\S+@\S+" took 43 s for 4,000 "@".
    started = time.perf_counter()
    fingerprint({"message": message})
    assert time.perf_counter() - started < 0.1


def test_server_waits_are_clamped():
    # A Retry-After past what a timer takes (or infinite) once stopped the thread.
    for value, pause in (
        ("inf", None),
        ("1e400", None),
        ("nan", None),
        ("soon", None),
        ("-5", 0.0),
        ("86400", MAX_WAIT),
        ("86401", MAX_WAIT),
        ("99999999999", MAX_WAIT),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),  # gone by
    ):
        d = Delivery(rng=lambda: 0.0)
        d.offer(req(b"x"), 0.0)
        dec = d.on_response(d.next(0.0), 503, {"retry-after": value}, 0.0)
        assert d.limits.get("") == pause, value
        # A day away is more than 5 minutes: the request is dropped, the pause holds.
        assert dec.retry is (pause is None or pause < 300) and d.wake_at() in (None, 0.5, 0.0), value
    limits = parse_rate_limits("inf:log, 1e12:span, nan:file, -5:error, 86401:session", 100.0)
    assert limits == {"span": 100.0 + MAX_WAIT, "error": 100.0, "session": 100.0 + MAX_WAIT}


def test_retry_after_may_be_an_http_date():
    now = 1_791_190_800.0
    for status in (429, 503):
        d = Delivery(rng=lambda: 0.0, clock=lambda: now)
        d.offer(req(b"x"), 0.0)
        later = email.utils.formatdate(now + 120, usegmt=True)
        assert d.on_response(d.next(0.0), status, {"retry-after": later}, 0.0).retry
        assert d.limits[""] == 120.0 and d.wake_at() == 120.0
    # A 429 without Fixwire-Rate-Limits pauses everything a minute at least.
    d = Delivery(rng=lambda: 0.0, clock=lambda: now)
    d.offer(req(b"x"), 0.0)
    d.on_response(d.next(0.0), 429, {"retry-after": email.utils.formatdate(now + 5, usegmt=True)}, 0.0)
    assert d.limits[""] == 60.0


def test_serializer_cuts_cycles_and_reads_only_what_it_keeps():
    s = Serializer(16)
    looped = []
    looped.extend([looped] * 100)  # unrolled to the depth limit: 100^10 values
    started = time.perf_counter()
    assert s(looped) == [CIRCULAR] * 100
    node = {"name": "a"}
    node["self"] = node
    cut = {"name": "a", "self": CIRCULAR}
    assert s({"node": node, "again": node}) == {"node": cut, "again": cut}, "shared, not cyclic: both kept"
    assert s(list(range(1_000_000))) == list(range(100))
    assert s(b"\xe2\x82\xac" * 1_000_000) == "€" * 4 + "..."  # 15 bytes: a fifth "€" would make 18
    assert time.perf_counter() - started < 0.5


def test_strings_are_cut_in_bytes_on_a_character_boundary():
    assert clip("a" * 1024, 1024) == "a" * 1024
    assert clip("a" * 1025, 1024) == "a" * 1021 + "..."
    # 2-byte characters: 1,024 bytes fit, 1,026 don't; the cut never splits one.
    assert clip("é" * 512, 1024) == "é" * 512
    cut = clip("é" * 513, 1024)
    assert cut == "é" * 510 + "..." and len(cut.encode()) == 1023
    cut = clip("a" + "€" * 400, 1024)  # 1 + 3 x 400 bytes
    assert cut == "a" + "€" * 340 + "..." and len(cut.encode()) == 1024
    cut = clip("\U0001f600" * 300, 1024)
    assert cut == "\U0001f600" * 255 + "..." and len(cut.encode()) == 1023
    assert clip("x" * 10, 0) == "x" * 10, "0: no limit"


def test_values_have_bounded_depth_breadth_and_size():
    s = Serializer()
    nested: dict = {}
    leaf = nested
    for _ in range(12):
        leaf["d"] = {}
        leaf["l"] = [[1]]
        leaf = leaf["d"]
    out = s(nested)
    for _ in range(9):
        out = out["d"]
    assert out == {"d": "[Object]", "l": "[Array]"}, "one deeper than 10 levels"
    assert s({"n": float("nan"), "i": float("inf"), "m": float("-inf"), "f": 1.5}) == {
        "n": "NaN",
        "i": "Infinity",
        "m": "-Infinity",
        "f": 1.5,
    }
    # 10,000 containers walked at most per value; the rest are markers.
    wide = [[[i] for i in range(100)] for _ in range(100)]  # 1 + 100 x 101 lists
    out = s(wide)
    assert out[:99] == wide[:99] and out[99] == "[Array]", "1 + 99 x 101 = 10,000 walked"
    assert s(wide[:50]) == wide[:50], "each value its own budget"

    class Broken:
        def __repr__(self):
            raise RuntimeError("no")

    class BrokenDict(dict):
        def items(self):
            raise RuntimeError("no")

    assert s([Broken(), BrokenDict(a=1)]) == ["[Unreadable]", "[Unreadable]"]


def test_a_message_stack_stops_at_the_frames_kept(monkeypatch):
    serialized = []
    serialize = event_builder.serialize_frame
    monkeypatch.setattr(event_builder, "serialize_frame", lambda *a: serialized.append(1) or serialize(*a))

    def recurse(n):
        return (
            event_builder.current_stacktrace(event_builder.Options(max_stack_frames=10)) if n == 0 else recurse(n - 1)
        )

    frames = recurse(200)["frames"]
    assert len(frames) == 10 and frames[-1]["function"] == "recurse"
    assert len(serialized) == 10, "the frames thrown away are never serialized"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_source_context_reads_regular_files_only(tmp_path):
    fifo = str(tmp_path / "app.py")
    os.mkfifo(fifo)  # opening it would block until a writer comes
    event = {"exception": {"values": [{"stacktrace": {"frames": [{"abs_path": fifo, "lineno": 1}]}}]}}
    event_builder.add_source_context(event, 100)
    assert "context_line" not in event["exception"]["values"][0]["stacktrace"]["frames"][0]


def test_source_lines_come_through_a_bounded_cache(tmp_path, monkeypatch):
    assert (event_builder.MAX_SOURCE_FILES, event_builder.MAX_SOURCE_CACHE_BYTES) == (64, 32 << 20)
    monkeypatch.setattr(event_builder, "_sources", collections.OrderedDict())
    monkeypatch.setattr(event_builder, "MAX_SOURCE_FILES", 3)

    def context_line(path):
        frame = {"abs_path": str(path), "lineno": 1}
        event_builder.add_source_context({"exception": {"values": [{"stacktrace": {"frames": [frame]}}]}}, 100)
        return frame.get("context_line")

    paths = []
    for i in range(5):
        paths.append(tmp_path / ("m%d.py" % i))
        paths[-1].write_text("a = %d\nb = 2\n" % i)
        assert context_line(paths[-1]) == "a = %d" % i
    assert list(event_builder._sources) == [str(p) for p in paths[2:]]
    big = tmp_path / "big.py"
    big.write_bytes(b"x = 1\n" + b"#" * event_builder.MAX_SOURCE_BYTES)  # 10 MB and 6 bytes
    assert context_line(big) is None
    # And at most MAX_SOURCE_CACHE_BYTES: two 12-byte files fit 24, a third
    # pushes the oldest out; one file over the budget stays alone.
    event_builder._sources.clear()
    monkeypatch.setattr(event_builder, "MAX_SOURCE_CACHE_BYTES", 24)
    for p in paths[:3]:
        context_line(p)
    assert list(event_builder._sources) == [str(p) for p in paths[1:3]]
    monkeypatch.setattr(event_builder, "MAX_SOURCE_CACHE_BYTES", 10)
    assert context_line(paths[3]) == "a = 3" and list(event_builder._sources) == [str(paths[3])]


def test_breadcrumbs_keep_the_last_ones_in_constant_time():
    s = scope.Scope(max_breadcrumbs=100)
    started = time.perf_counter()
    for i in range(200_000):
        s.add_breadcrumb({"message": str(i)})
    assert time.perf_counter() - started < 0.5
    assert [c["message"] for c in s.breadcrumbs] == [str(i) for i in range(199_900, 200_000)]


def test_the_event_queue_drops_new_events_when_full():
    from fixwire._core.pipeline import EventQueue

    q = EventQueue(2)
    assert [q.put({"n": i}) for i in range(3)] == [True, True, False]
    assert [e["n"] for e in q.drain()] == [0, 1] and q.overflowed == 1


def req(body, category="error"):
    return Outbound("/v1/logs", "application/json", body, category)


def test_delivery_retries():
    d = Delivery(rng=lambda: 1.0)
    d.offer(req(b"x"), 0.0)
    sent = d.next(0.0)
    assert d.on_error(sent, 0.0).retry and d.next(0.1) is None
    # 1 s, then twice as long each time: 3 retries, then the request goes.
    waits = [d.wake_at()]
    for _ in range(MAX_ATTEMPTS - 1):
        now = d.wake_at()
        dec = d.on_response(d.next(now), 503, {}, now)
        waits.append(d.wake_at() - now if dec.retry else None)
    assert MAX_ATTEMPTS == 4 and waits == [1.0, 2.0, 4.0, None] and dec.dropped and d.empty()
    d = Delivery(rng=lambda: 0.0)
    d.offer(req(b"x"), 0.0)
    assert d.on_error(d.next(0.0), 0.0).retry and d.wake_at() == pytest.approx(0.5), "jitter halves at worst"

    # A 5xx with Retry-After pauses all data that long; the request waits too.
    d = Delivery(rng=lambda: 0.0)
    d.offer(req(b"busy"), 0.0)
    assert d.on_response(d.next(0.0), 503, {"retry-after": "30"}, 0.0).retry
    d.offer(req(b"span", "span"), 1.0)
    assert d.next(29.0) is None and d.next(30.0).body == b"busy" and d.next(30.0).body == b"span"
    d.offer(req(b"broken"), 0.0)
    assert d.on_response(d.next(30.0), 500, {}, 30.0).retry and d.next(100.0).body == b"broken"

    # A next try more than 5 minutes away drops the request.
    d.offer(req(b"later"), 100.0)
    assert d.on_response(d.next(100.0), 503, {"retry-after": "301"}, 100.0).dropped and d.empty()
    d.limits.clear()
    d.offer(req(b"soon"), 100.0)
    assert d.on_response(d.next(100.0), 503, {"retry-after": "300"}, 100.0).retry

    # Other 4xx are final.
    d = Delivery()
    for status in (400, 401, 403, 404, 413, 415):
        d.offer(req(b"y"), 0.0)
        assert d.on_response(d.next(0.0), status, {}, 0.0).dropped and d.empty()


@pytest.mark.parametrize(
    "answers",
    [
        [429, 429, 429, 429],
        [None, 503, 429, 500],
        [429, None, None, 502],
        ["limited", 503, "limited", None],
    ],
)
def test_a_request_is_sent_at_most_4_times_a_429s_retry_included(answers):
    d = Delivery(rng=lambda: 1.0)
    item = req(b"x")
    d.offer(item, 0.0)
    now, sent = 0.0, 0
    for answer in answers:
        assert d.next(now) is item
        sent += 1
        if answer is None:
            dec = d.on_error(item, now)
        elif answer == "limited":
            dec = d.on_response(item, 429, {"retry-after": "1", "fixwire-rate-limits": "1:error"}, now)
        else:
            dec = d.on_response(item, int(answer), {}, now)
        assert dec.retry is (sent < MAX_ATTEMPTS), answers
        now = d.wake_at() or now
    assert sent == MAX_ATTEMPTS == 4 and dec.dropped and d.empty()


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
    # A 429 that names no data pauses all of it, a minute at least.
    d.offer(req(b"busy"), 200.0)
    d.on_response(d.next(200.0), 429, {"retry-after": "2"}, 200.0)
    assert d.limited("span", 259.0) and not d.limited("span", 260.0)
    d.offer(req(b"later"), 300.0)
    d.on_response(d.next(300.0), 429, {"retry-after": "120"}, 300.0)
    assert d.limited("span", 419.0) and not d.limited("span", 420.0)
    # So does an empty category list (the server's "busy").
    d.on_response(req(b"x"), 200, {"fixwire-rate-limits": "5:"}, 500.0)
    assert d.limited("check_in", 504.0)
    # Data paused for more than 5 minutes isn't queued.
    d.on_response(req(b"x"), 200, {"fixwire-rate-limits": "301:span"}, 600.0)
    assert not d.offer(req(b"spans", "span"), 600.0) and d.offer(req(b"error"), 600.0)


def test_rate_limit_header():
    limits = parse_rate_limits("60:log;span, 3600:file, 10:, bad, x:error", 100.0)
    assert limits == {"log": 160.0, "span": 160.0, "file": 3700.0, "": 110.0}
    # Categories Fixwire doesn't name are ignored (and never mean "all").
    assert parse_rate_limits("60:log;metric_bucket, 30:profile", 0.0) == {"log": 60.0}


def test_a_full_queue_drops_new_data():
    d = Delivery(max_items=2, rng=lambda: 0.0)
    assert [d.offer(req(b), 0.0) for b in (b"1", b"2", b"3")] == [True, True, False]
    assert [i.body for i in d.queue] == [b"1", b"2"] and d.overflowed == 1
    # As many again may wait for a retry; past that, a failed request is dropped.
    one, two = d.next(0.0), d.next(0.0)
    assert d.offer(req(b"3"), 0.0) and d.offer(req(b"4"), 0.0)
    three = d.next(0.0)
    assert d.on_error(one, 0.0).retry and d.on_error(two, 0.0).retry
    assert d.on_error(three, 0.0).dropped
    assert [i.body for i in d.queue] == [b"2", b"1", b"4"] and d.retrying == 2
    assert d.offer(req(b"5"), 0.0) and not d.offer(req(b"6"), 0.0)
    assert [i.body for i in d.take()] == [b"2", b"1", b"4", b"5"] and d.retrying == 0 and d.queued_bytes == 0
