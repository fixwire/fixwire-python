import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fixwire
from fixwire._core import scope

KEY = "publickey"


class Ingest:
    """A fake Fixwire ingest (protocol v1): records requests, answers as
    scripted, and reads them the way the server does."""

    def __init__(self):
        self.requests = []
        self.responses = []  # (status, headers) to answer with, in order; then 200
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        ingest = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                parsed = json.loads(body) if self.headers.get("Content-Type") == "application/json" else None
                with ingest.cond:
                    ingest.requests.append(
                        {"path": self.path, "headers": dict(self.headers), "body": body, "json": parsed}
                    )
                    status, headers = ingest.responses.pop(0) if ingest.responses else (200, {})
                    ingest.cond.notify_all()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.dsn = "http://%s@127.0.0.1:%d" % (KEY, self.server.server_address[1])

    def bodies(self, path):
        """The JSON bodies sent to a path (a prefix for check-ins)."""
        with self.lock:
            return [r["json"] for r in self.requests if r["path"].startswith(path)]

    def records(self):
        """Log records sent to /v1/logs: (record with plain attributes, resource attributes)."""
        out = []
        for body in self.bodies("/v1/logs"):
            for rl in body["resourceLogs"]:
                res = attrs(rl["resource"]["attributes"])
                for sl in rl["scopeLogs"]:
                    for r in sl["logRecords"]:
                        out.append(({**r, "attributes": attrs(r.get("attributes", []))}, res))
        return out

    def events(self):
        """The errors and messages, as the server turns them into events."""
        return [server_event(r, res) for r, res in self.records() if r.get("eventName") in EVENT_NAMES]

    def otlp_spans(self):
        """Spans sent to /v1/traces: (span, resource attributes)."""
        out = []
        for body in self.bodies("/v1/traces"):
            for rs in body["resourceSpans"]:
                res = attrs(rs["resource"]["attributes"])
                for ss in rs["scopeSpans"]:
                    out.extend((s, res) for s in ss["spans"])
        return out

    def spans(self):
        """The spans, as the server reads them."""
        return [server_span(s, res) for s, res in self.otlp_spans()]

    def wait(self, n, timeout=5.0):
        with self.cond:
            self.cond.wait_for(lambda: len(self.requests) >= n, timeout)
            return len(self.requests)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# How the server reads OTLP (server/features/ingest/otlp): attributes as
# plain values, errors and messages as events, spans as span records.

EVENT_NAMES = ("exception", "fixwire.message")

FRAME_KEYS = {
    "function": "function",
    "module": "module",
    "file": "filename",
    "abs_path": "abs_path",
    "line": "lineno",
    "column": "colno",
    "in_app": "in_app",
    "context_line": "context_line",
    "pre_context": "pre_context",
    "post_context": "post_context",
}
CONSUMED = {
    "exception.type",
    "exception.message",
    "exception.stacktrace",
    "user.id",
    "user.email",
    "user.name",
    "client.address",
    "http.request.method",
    "url.full",
    "http.route",
    "user_agent.original",
}


def plain(value):
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        return [plain(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return attrs(value["kvlistValue"].get("values", []))
    return None


def attrs(kvs):
    return {kv["key"]: plain(kv["value"]) for kv in kvs}


def level(kind, severity):
    for floor, name in ((21, "fatal"), (17, "error"), (13, "warning"), (9, "info"), (1, "debug")):
        if severity >= floor:
            return name
    return "info" if kind == "fixwire.message" else "error"


def server_event(record, res):
    a = record["attributes"]
    e = {
        "event_id": a.get("fixwire.event_id"),
        "timestamp": int(record["timeUnixNano"]) / 1e9,
        "level": level(record.get("eventName"), record.get("severityNumber", 0)),
        "release": res.get("service.version", ""),
        "environment": res.get("deployment.environment.name", ""),
        "transaction": a.get("fixwire.transaction") or a.get("http.route") or "",
        "sdk": {"name": res.get("telemetry.sdk.name"), "version": res.get("telemetry.sdk.version")},
    }
    if res.get("host.name"):
        e["server_name"] = res["host.name"]
    if "fixwire.tags" in a:
        e["tags"] = a["fixwire.tags"]
    if "fixwire.fingerprint" in a:
        e["fingerprint"] = a["fixwire.fingerprint"]
    user = {k: a[f] for k, f in (("id", "user.id"), ("email", "user.email"), ("username", "user.name")) if f in a}
    if "client.address" in a:
        user["ip_address"] = a["client.address"]
    if user:
        e["user"] = user
    contexts = dict(a.get("fixwire.contexts") or {})
    if record.get("traceId"):
        contexts["trace"] = {"trace_id": record["traceId"], "span_id": record.get("spanId", "")}
    if a.get("fixwire.suppressed"):
        contexts["fixwire"] = {"suppressed": {"count": a["fixwire.suppressed"]}}
    e["contexts"] = contexts
    if "fixwire.breadcrumbs" in a:
        e["breadcrumbs"] = {"values": a["fixwire.breadcrumbs"]}
    if a.get("http.request.method") or a.get("url.full") or a.get("user_agent.original"):
        e["request"] = {"method": a.get("http.request.method", ""), "url": a.get("url.full", "")}
        if a.get("user_agent.original"):
            e["request"]["headers"] = {"User-Agent": a["user_agent.original"]}
    extra = {k: v for k, v in a.items() if k not in CONSUMED and not k.startswith("fixwire.")}
    if extra:
        e["extra"] = extra
    body = (record.get("body") or {}).get("stringValue", "")
    if record["eventName"] == "fixwire.message":
        e["message"] = body
        return e
    if body:
        e["message"] = body
    handled = a.get("fixwire.handled", True)
    values = []
    chain = a.get("fixwire.exceptions") or []
    for i in range(len(chain) - 1, -1, -1):  # the processor's order: outermost last
        x = chain[i]
        v = {"type": x.get("type"), "value": x.get("message")}
        if "module" in x:
            v["module"] = x["module"]
        mech = dict(x.get("mechanism") or {"type": "generic"})
        if "handled" not in mech and i == 0:
            mech["handled"] = handled
        v["mechanism"] = mech
        if "frames" in x:
            v["stacktrace"] = {"frames": [{FRAME_KEYS[k]: f[k] for k in FRAME_KEYS if k in f} for f in x["frames"]]}
        values.append(v)
    e["exception"] = {"values": values}
    return e


def server_span(s, res):
    a = attrs(s.get("attributes", []))
    for k, v in res.items():
        a.setdefault(k, v)
    parent = s.get("parentSpanId", "")
    flags = s.get("flags", 0)
    out = {
        "trace_id": s["traceId"],
        "span_id": s["spanId"],
        "name": s["name"],
        "kind": s["kind"],
        "status": "error" if s.get("status", {}).get("code") == 2 else "ok",
        "is_segment": not parent or (flags & 0x300) == 0x300,
        "start_timestamp": int(s["startTimeUnixNano"]) / 1e9,
        "end_timestamp": int(s["endTimeUnixNano"]) / 1e9,
        "attributes": a,
    }
    if parent:
        out["parent_span_id"] = parent
    return out


@pytest.fixture
def ingest():
    srv = Ingest()
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def clean_scopes():
    """Each test starts with empty global and default scopes."""
    for s in (scope.get_global_scope(), scope.get_isolation_scope(), scope.get_current_scope()):
        s.tags.clear()
        s.extras.clear()
        s.contexts.clear()
        s.breadcrumbs.clear()
        s.processors.clear()
        s.user = s.level = s.fingerprint = s.propagation = None
    yield
    fixwire.close(0.5)
