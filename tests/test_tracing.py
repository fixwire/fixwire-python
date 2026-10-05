"""Tracing: span trees, sampling, propagation, and the integrations' spans."""

import pytest

import fixwire
from fixwire._core.tracing import PropagationContext, keep, sample_rand

TRACE, PARENT = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"


def span_items(ingest):
    return ingest.spans()


def attr(span, key):
    return span["attributes"].get(key)


def test_a_segment_and_its_spans_go_out_together(ingest):
    fixwire.init(ingest.dsn, traces_sample_rate=1.0, release="api@1.0.0", default_integrations=False)
    with fixwire.start_span("checkout", op="task") as seg:
        with fixwire.start_span("SELECT * FROM carts WHERE email = 'ada@example.com'", op="db.query"):
            pass
        with fixwire.start_span("POST /charge", op="http.client") as charge:
            charge.set_attribute("url.full", "https://pay.example.com/charge?email=ada@example.com")
            try:
                raise ValueError("declined")
            except ValueError:
                fixwire.capture_exception()
    assert fixwire.flush(5)
    traces = [r for r in ingest.requests if r["path"] == "/v1/traces"]
    assert len(traces) == 1, "one request per segment"
    assert traces[0]["headers"]["Authorization"] == "Bearer publickey"
    spans = span_items(ingest)
    assert [s["name"] for s in spans] == [
        "checkout",
        "SELECT * FROM carts WHERE email = '[REDACTED:email]'",
        "POST /charge",
    ]
    root, db, http = spans
    assert root["is_segment"] and not db["is_segment"]
    assert db["parent_span_id"] == http["parent_span_id"] == root["span_id"] == seg.span_id
    assert attr(db, "fixwire.op") == "db.query" and attr(http, "fixwire.op") == "http.client"
    assert (root["kind"], db["kind"], http["kind"]) == (1, 3, 3)  # internal, client, client
    # Release, environment and the SDK are the resource's.
    assert attr(root, "service.version") == "api@1.0.0" and attr(root, "telemetry.sdk.name") == "fixwire.python"
    assert attr(root, "deployment.environment.name") == "production"
    assert {k for k in root["attributes"] if k.startswith("fixwire.")} == {"fixwire.op", "fixwire.origin"}
    assert attr(http, "url.full") == "https://pay.example.com/charge?email=[REDACTED:email]"
    assert root["end_timestamp"] >= http["end_timestamp"] >= http["start_timestamp"] >= root["start_timestamp"]
    raw = {s["spanId"]: s for s, _ in ingest.otlp_spans()}
    assert raw[root["span_id"]]["flags"] == 0x101 and "parentSpanId" not in raw[root["span_id"]]
    assert raw[db["span_id"]]["flags"] == 0x101 and raw[db["span_id"]]["status"] == {"code": 1}
    # The error links to the span it happened in.
    [(record, _)] = ingest.records()
    assert record["traceId"] == root["trace_id"] and record["spanId"] == http["span_id"]
    [event] = ingest.events()
    assert event["contexts"]["trace"] == {"trace_id": root["trace_id"], "span_id": http["span_id"]}
    assert event["transaction"] == "checkout"


def test_sampling(ingest):
    fixwire.init(ingest.dsn, traces_sample_rate=0.0, default_integrations=False)
    with fixwire.start_span("not sampled"):
        with fixwire.start_span("child") as child:
            assert not child.sampled
    fixwire.close(1)
    fixwire.init(ingest.dsn, default_integrations=False, traces_sampler=lambda ctx: ctx["name"] == "keep")
    for name in ("drop", "keep"):
        with fixwire.isolation_scope():
            with fixwire.start_span(name):
                pass
    assert fixwire.flush(5)
    assert [s["name"] for s in span_items(ingest)] == ["keep"]


def test_continuing_and_propagating_traces(ingest):
    fixwire.init(
        ingest.dsn,
        traces_sample_rate=0.0,
        default_integrations=False,
        trace_propagation_targets=["api.internal.example"],
    )
    with fixwire.isolation_scope():
        # The caller sampled the trace: we record it although our rate is 0.
        fixwire.continue_trace(
            {
                "traceparent": "00-%s-%s-01" % (TRACE, PARENT),
                "tracestate": "congo=t61rcWkgMzE,vendor=a",
                "baggage": "tenant=acme,user=7",
            }
        )
        with fixwire.start_span("GET /orders") as seg:
            headers = fixwire.trace_headers("tenant=globex")
            fixwire.capture_message("inside")
    assert seg.trace_id == TRACE and seg.parent_span_id == PARENT and seg.sampled and seg.parent_remote
    assert headers == {
        "traceparent": "00-%s-%s-01" % (TRACE, seg.span_id),
        "tracestate": "congo=t61rcWkgMzE,vendor=a",  # passed on unchanged
        "baggage": "tenant=globex,user=7",  # the request's own first, then the incoming members
    }
    with fixwire.isolation_scope():
        fixwire.continue_trace({"Traceparent": "00-%s-%s-00" % (TRACE, PARENT)})
        with fixwire.start_span("unsampled upstream") as s2:
            assert s2.trace_id == TRACE and not s2.sampled
            assert fixwire.trace_headers()["traceparent"].endswith("-00")
    with fixwire.isolation_scope():
        # Invalid or missing: a new trace (the old header is not read at all).
        for headers in ({"traceparent": "00-%s-%s-01" % ("0" * 32, PARENT)}, {"x-other-trace": TRACE + "-1"}):
            assert not fixwire.continue_trace(headers).continued
    assert fixwire.should_propagate("https://api.internal.example/v1") and not fixwire.should_propagate(
        "https://evil.example"
    )
    assert fixwire.flush(5)
    [segment] = [s for s in span_items(ingest) if s["is_segment"]]
    assert segment["parent_span_id"] == PARENT
    raw = {s["spanId"]: s for s, _ in ingest.otlp_spans()}
    assert raw[segment["span_id"]]["flags"] == 0x301, "a remote parent"
    assert ingest.events()[0]["contexts"]["trace"]["trace_id"] == TRACE


def test_root_decisions_come_from_the_trace_id(ingest):
    # The trace id's last 56 bits as a fraction of 1, kept when at least
    # 1 - rate: every service (and the JavaScript SDK) decides alike.
    low, mid, high = ("%018x%014x" % (0, r) for r in (0x10, 1 << 55, (1 << 56) - 1))
    assert sample_rand(low) == 0x10 / 2**56 and PropagationContext(trace_id=mid).sample_rand == 0.5
    assert not keep(low, 0.5) and keep(mid, 0.5) and keep(high, 0.5)
    assert keep(low, 1.0) and not keep(mid, 0.0) and not keep(low, 0.0)

    fixwire.init(ingest.dsn, traces_sample_rate=0.25, default_integrations=False)
    kept = dropped = 0
    for _ in range(200):
        with fixwire.start_span("job") as s:
            assert s.sampled == (sample_rand(s.trace_id) >= 0.75)
            kept += s.sampled
            dropped += not s.sampled
            assert "tracestate" not in fixwire.trace_headers(), "none of our own"
    assert kept and dropped


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_asgi_request_spans(ingest):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from fixwire.integrations.asgi import FixwireMiddleware

    async def item(request):
        with fixwire.start_span("load item", op="db.query"):
            pass
        if request.path_params["id"] == "0":
            raise ValueError("item 0 is broken")
        return PlainTextResponse("ok")

    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False)
    app = Starlette(routes=[Route("/items/{id}", item)])
    app.add_middleware(FixwireMiddleware)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/items/7").status_code == 200
        assert client.get("/items/0", headers={"traceparent": "00-%s-%s-01" % (TRACE, PARENT)}).status_code == 500
    assert fixwire.flush(5)
    segments = [s for s in span_items(ingest) if s["is_segment"]]
    assert [s["name"] for s in segments] == ["GET /items/{id}", "GET /items/{id}"]
    ok, failed = segments
    assert attr(ok, "http.response.status_code") == 200 and ok["status"] == "ok"
    assert attr(ok, "fixwire.op") == "http.server" and ok["kind"] == 2
    assert failed["status"] == "error" and failed["trace_id"] == TRACE and failed["parent_span_id"] == PARENT
    assert attr(failed, "http.route") == "/items/{id}"
    [event] = ingest.events()
    assert event["contexts"]["trace"]["trace_id"] == TRACE and event["transaction"] == "/items/{id}"


def test_httpx_child_spans_and_targets(ingest):
    import httpx

    from fixwire.integrations.httpx import HttpxIntegration

    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("traceparent"), request.headers.get("tracestate")))
        return httpx.Response(503 if "fail" in request.url.path else 200)

    fixwire.init(
        ingest.dsn,
        traces_sample_rate=1.0,
        default_integrations=False,
        integrations=[HttpxIntegration()],
        trace_propagation_targets=["api.internal.example"],
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.get("https://api.internal.example/a")  # outside a span: headers, no span
    with fixwire.start_span("job") as seg:
        client.get("https://api.internal.example/fail?token=abc")
        client.get("https://third-party.example/b")
    assert fixwire.flush(5)
    assert seen[0][1] is not None and seen[1][1].startswith("00-" + seg.trace_id) and seen[2][1] is None
    assert seen[1][2] is None  # a trace of our own: no tracestate to pass on
    children = [s for s in span_items(ingest) if not s["is_segment"]]
    assert [s["name"] for s in children] == [
        "GET https://api.internal.example/fail",
        "GET https://third-party.example/b",
    ]
    assert children[0]["status"] == "error" and attr(children[0], "http.response.status_code") == 503
    # The receiver continues from the client span, not from its parent.
    assert seen[1][1] == "00-%s-%s-01" % (seg.trace_id, children[0]["span_id"])


def test_requests_child_spans_breadcrumbs_and_targets(ingest):
    import requests
    from requests.adapters import BaseAdapter

    from fixwire.integrations.requests import RequestsIntegration

    seen = []

    class Adapter(BaseAdapter):
        def send(self, request, **kwargs):
            seen.append((request.url, request.headers.get("traceparent"), request.headers.get("baggage")))
            if "down" in request.url:
                raise requests.ConnectionError("connection refused")
            response = requests.Response()
            response.status_code = 404 if "missing" in request.url else 200
            response.url = request.url
            return response

        def close(self):
            pass

    fixwire.init(
        ingest.dsn,
        traces_sample_rate=1.0,
        default_integrations=False,
        integrations=[RequestsIntegration()],
        trace_propagation_targets=["api.internal.example"],
    )
    session = requests.Session()
    session.mount("https://", Adapter())
    with fixwire.isolation_scope():
        fixwire.continue_trace({"baggage": "tenant=acme"})
        with fixwire.start_span("sync orders") as seg:
            session.get("https://api.internal.example/orders?page=2", headers={"baggage": "vendor=1"})
            session.get("https://api.internal.example/missing")
            with pytest.raises(requests.ConnectionError):
                session.get("https://api.internal.example/down")
            session.get("https://third-party.example/rates")
            fixwire.capture_message("synced")
    assert fixwire.flush(5)
    children = [s for s in span_items(ingest) if not s["is_segment"]]
    assert [s["name"] for s in children] == [
        "GET https://api.internal.example/orders",
        "GET https://api.internal.example/missing",
        "GET https://api.internal.example/down",
        "GET https://third-party.example/rates",
    ]
    assert [s["status"] for s in children] == ["ok", "error", "error", "ok"]
    assert attr(children[2], "error.type") == "ConnectionError"
    assert attr(children[0], "fixwire.origin") == "auto.http.requests"
    assert seen[0][1] == "00-%s-%s-01" % (seg.trace_id, children[0]["span_id"])
    assert seen[0][2] == "vendor=1,tenant=acme" and seen[1][2] == "tenant=acme"
    assert seen[3][1] is None  # not a propagation target
    [event] = ingest.events()
    crumbs = [b for b in event["breadcrumbs"]["values"] if b.get("category") == "http"]
    assert [(b["data"]["url"], b["data"].get("status_code"), b["level"]) for b in crumbs] == [
        ("https://api.internal.example/orders", 200, "info"),
        ("https://api.internal.example/missing", 404, "info"),
        ("https://api.internal.example/down", None, "error"),
        ("https://third-party.example/rates", 200, "info"),
    ]


def test_celery_task_spans(ingest):
    from celery import Celery

    from fixwire.integrations.celery import CeleryIntegration

    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False, integrations=[CeleryIntegration()])
    app = Celery("t", broker="memory://")
    app.conf.task_always_eager = True

    @app.task(name="reports.build")
    def build(n):
        with fixwire.start_span("render", op="function"):
            return n * 2

    build.delay(21)
    assert fixwire.flush(5)
    spans = span_items(ingest)
    assert [(s["name"], s["is_segment"]) for s in spans] == [("reports.build", True), ("render", False)]
    assert attr(spans[0], "fixwire.op") == "queue.process" and attr(spans[0], "messaging.system") == "celery"
    assert spans[0]["kind"] == 5  # consumer


def test_celery_carries_the_trace_in_message_headers():
    from types import SimpleNamespace

    from fixwire.integrations import celery

    with fixwire.isolation_scope():
        fixwire.continue_trace({"traceparent": "00-%s-%s-01" % (TRACE, PARENT), "tracestate": "vendor=a"})
        headers = {}
        celery._publish(headers=headers)
    assert headers["traceparent"].startswith("00-%s-" % TRACE) and headers["tracestate"] == "vendor=a"
    request = SimpleNamespace(headers=headers, get=lambda k: None)
    assert celery._incoming(request) == headers


def test_local_roots_sample_and_trace_independently(ingest):
    fixwire.init(ingest.dsn, traces_sample_rate=0.5, default_integrations=False)
    roots = []
    for i in range(300):
        with fixwire.start_span("job %d" % i) as s:
            roots.append(s)
    sampled = sum(s.sampled for s in roots)
    assert 90 < sampled < 210  # each root decided on its own
    assert len({s.trace_id for s in roots}) == 300
    fixwire.close(1)


def test_big_segments_are_split_under_the_request_limit(ingest, monkeypatch):
    from fixwire._core import pipeline

    monkeypatch.setattr(pipeline, "MAX_SPANS_BYTES", 4000)
    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False)
    with fixwire.start_span("import"):
        for i in range(30):
            with fixwire.start_span("row %d" % i, attributes={"payload": "x" * 100}):
                pass
    assert fixwire.flush(5)
    traces = [r for r in ingest.requests if r["path"] == "/v1/traces"]
    assert len(traces) > 1 and all(len(r["body"]) <= 4000 for r in traces)
    assert len(span_items(ingest)) == 31
