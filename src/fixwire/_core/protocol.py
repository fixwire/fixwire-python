"""Fixwire protocol v1 on the wire (sdks/PROTOCOL.md): errors, messages and
spans travel as OTLP/HTTP JSON on /v1/logs and /v1/traces; sessions,
check-ins and feedback as small JSON bodies on their own endpoints.

Everything here is pure: it turns the SDK's own shapes (events as
before_send sees them, span records) into request bodies. In OTLP JSON,
attributes are key-value lists, ids are hex and 64-bit integers strings.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import time
from collections.abc import Mapping
from typing import Any, cast

from fixwire._core.dsn import SDK_NAME
from fixwire._core.jsonish import as_dict, as_list, dicts, get_dict
from fixwire._version import __version__

#: The endpoints, relative to the DSN's base URL.
TRACES = "/v1/traces"
LOGS = "/v1/logs"
SESSIONS = "/v1/sessions"
FEEDBACK = "/v1/feedback"
CHECK_INS = "/v1/check-ins/"

#: OpenTelemetry severity (number, text) of each event level.
SEVERITY = {
    "debug": (5, "DEBUG"),
    "info": (9, "INFO"),
    "warning": (13, "WARN"),
    "error": (17, "ERROR"),
    "fatal": (21, "FATAL"),
}

#: OTLP span kinds.
INTERNAL, SERVER, CLIENT, PRODUCER, CONSUMER = 1, 2, 3, 4, 5

#: OTLP span flags: W3C's sampled flag, then whether the parent's
#: remoteness is known (bit 8) and whether it is remote (bit 9).
SAMPLED, HAS_IS_REMOTE, IS_REMOTE = 0x01, 0x100, 0x200

#: Status codes.
STATUS_OK, STATUS_ERROR = 1, 2

#: Span kinds by operation, the JavaScript SDK's rules (and serverless
#: functions as servers).
_KINDS = (
    (re.compile(r"^(?:http|rpc)\.server|^function\.aws\.lambda"), SERVER),
    (re.compile(r"^(?:http|rpc)\.client|^db|^cache|^gen_ai\.(?:chat|embeddings)"), CLIENT),
    (re.compile(r"^queue\.publish"), PRODUCER),
    (re.compile(r"^queue\.process"), CONSUMER),
)

_SCOPE = {"name": SDK_NAME, "version": __version__}
_HEX_ID = re.compile(r"^[0-9a-f]+$")
_INT64 = 1 << 63

#: Frame keys: the SDK's (and before_send's) names → the protocol's.
_FRAME_KEYS = (
    ("function", "function"),
    ("module", "module"),
    ("filename", "file"),
    ("abs_path", "abs_path"),
    ("lineno", "line"),
    ("colno", "column"),
    ("in_app", "in_app"),
    ("context_line", "context_line"),
    ("pre_context", "pre_context"),
    ("post_context", "post_context"),
    ("vars", "vars"),
)
_CRUMB_KEYS = ("timestamp", "type", "category", "message", "level", "data")
#: The user, in OpenTelemetry's names; other keys go as user.<key>.
_USER_KEYS = (("id", "user.id"), ("email", "user.email"), ("username", "user.name"), ("ip_address", "client.address"))


def dumps(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str).encode("utf-8")


def compress(body: bytes) -> bytes:
    return gzip.compress(body, compresslevel=6)


def sdk() -> dict[str, str]:
    """The ``sdk`` of a Fixwire JSON body."""
    return {"name": SDK_NAME, "version": __version__}


def nanos(seconds: float) -> str:
    """Unix seconds as OTLP's nanoseconds, to the microsecond."""
    return str(round(seconds * 1_000_000) * 1000)


def any_value(value: object) -> dict[str, Any]:
    """A value as an OTLP AnyValue: maps become key-value lists, lists
    arrays, anything else unknown a string."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)} if -_INT64 <= value < _INT64 else {"stringValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value} if math.isfinite(value) else {"stringValue": str(value)}
    if isinstance(value, str):
        return {"stringValue": value}
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {"kvlistValue": {"values": key_values(cast("Mapping[object, Any]", value))}}
    if isinstance(value, (list, tuple)):
        items = cast("list[object] | tuple[object, ...]", value)
        return {"arrayValue": {"values": [any_value(v) for v in items]}}
    return {"stringValue": str(value)}


def key_values(attrs: Mapping[Any, Any]) -> list[dict[str, Any]]:
    """OTLP attributes (a key-value list); None values are left out."""
    return [{"key": str(k), "value": any_value(v)} for k, v in attrs.items() if v is not None]


def service_name(release: str | None) -> str | None:
    """The service's name, when known: OTEL_SERVICE_NAME, else the name in
    a "name@version" release."""
    name = os.environ.get("OTEL_SERVICE_NAME")
    if name:
        return name
    if release and "@" in release:
        return release.split("@", 1)[0] or None
    return None


def resource(service: str | None, release: str | None, environment: str | None, host: str | None) -> dict[str, Any]:
    """The OTLP resource: who sends, and the release and environment."""
    return {
        "attributes": key_values(
            {
                "service.name": service or None,
                "service.version": release or None,
                "deployment.environment.name": environment or None,
                "host.name": host or None,
                "telemetry.sdk.name": SDK_NAME,
                "telemetry.sdk.version": __version__,
                "telemetry.sdk.language": "python",
            }
        )
    }


def logs(res: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """An ExportLogsServiceRequest."""
    return {"resourceLogs": [{"resource": res, "scopeLogs": [{"scope": _SCOPE, "logRecords": records}]}]}


def traces(res: dict[str, Any], spans: list[dict[str, Any]]) -> dict[str, Any]:
    """An ExportTraceServiceRequest."""
    return {"resourceSpans": [{"resource": res, "scopeSpans": [{"scope": _SCOPE, "spans": spans}]}]}


# Errors and messages (PROTOCOL.md §4).


def log_record(event: dict[str, Any]) -> dict[str, Any]:
    """An error (an event with exceptions) or a message as an OTLP log
    record. The event is the serialized, redacted one."""
    values = dicts(get_dict(event.get("exception")).get("values"))
    level = event.get("level")
    number, text = SEVERITY.get(level if isinstance(level, str) else "", SEVERITY["error"])
    timestamp = event.get("timestamp")
    record: dict[str, Any] = {
        "timeUnixNano": nanos(float(timestamp) if isinstance(timestamp, (int, float)) else time.time()),
        "eventName": "exception" if values else "fixwire.message",
        "severityNumber": number,
        "severityText": text,
    }
    contexts = get_dict(event.get("contexts"))
    trace = get_dict(contexts.get("trace"))
    if _hex(trace.get("trace_id"), 32):
        record["traceId"] = trace["trace_id"]
        if _hex(trace.get("span_id"), 16):
            record["spanId"] = trace["span_id"]
    body = _message(event)
    if body:
        record["body"] = {"stringValue": body}

    # Extra data first: what Fixwire doesn't know, it keeps as the event's extra.
    attrs: dict[str, Any] = dict(get_dict(event.get("extra")))
    if values:
        outermost = values[-1]  # the SDK keeps causes first; the protocol wants the one caught first
        attrs["exception.type"] = outermost.get("type")
        attrs["exception.message"] = outermost.get("value")
        attrs["fixwire.exceptions"] = [_exception(v) for v in reversed(values)]
        attrs["fixwire.handled"] = not any(get_dict(v.get("mechanism")).get("handled") is False for v in values)
    attrs["fixwire.event_id"] = event.get("event_id")
    tags = {str(k): str(v) for k, v in get_dict(event.get("tags")).items() if v is not None}
    if event.get("logger"):
        tags.setdefault("logger", str(event["logger"]))
    attrs["fixwire.tags"] = tags or None
    attrs["fixwire.fingerprint"] = [str(x) for x in as_list(event.get("fingerprint"))] or None
    # The trace is the record's own; the SDK's own context says what was suppressed.
    attrs["fixwire.contexts"] = {k: v for k, v in contexts.items() if k not in ("trace", "fixwire")} or None
    count = get_dict(get_dict(contexts.get("fixwire")).get("suppressed")).get("count")
    attrs["fixwire.suppressed"] = count if isinstance(count, int) and count > 0 else None
    crumbs = dicts(get_dict(event.get("breadcrumbs")).get("values"))
    attrs["fixwire.breadcrumbs"] = [{k: c[k] for k in _CRUMB_KEYS if c.get(k) is not None} for c in crumbs] or None
    attrs["fixwire.transaction"] = event.get("transaction")
    user = dict(get_dict(event.get("user")))
    for key, name in _USER_KEYS:
        value = user.pop(key, None)
        attrs[name] = str(value) if value is not None and value != "" else None
    for key, value in user.items():
        attrs["user.%s" % key] = value
    request = get_dict(event.get("request"))
    url, query = request.get("url"), request.get("query_string")
    attrs["http.request.method"] = request.get("method")
    attrs["url.full"] = "%s?%s" % (url, query) if url and query else url
    for key, value in get_dict(request.get("headers")).items():
        # The allowlisted headers; the user agent is what crawler filters read.
        name = str(key).lower()
        attrs["user_agent.original" if name == "user-agent" else "http.request.header." + name] = value
    record["attributes"] = key_values(attrs)
    return record


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and bool(_HEX_ID.match(value))


def _message(event: dict[str, Any]) -> str:
    """The message of a message event, or the log line of a logged error."""
    msg: Any = event.get("message")
    m = as_dict(msg)
    if m is not None:
        msg = m.get("formatted") or m.get("message")
    if not msg:
        entry = get_dict(event.get("logentry"))
        msg = entry.get("formatted") or entry.get("message")
    return str(msg) if msg else ""


def _exception(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": value.get("type"),
        "message": value.get("value"),
        "module": value.get("module"),
        "mechanism": as_dict(value.get("mechanism")),
    }
    frames = dicts(get_dict(value.get("stacktrace")).get("frames"))
    if frames:
        # Oldest call first, the raising line last: the SDK's order already.
        out["frames"] = [{to: f[k] for k, to in _FRAME_KEYS if f.get(k) is not None} for f in frames]
    return out


# Spans (PROTOCOL.md §3).


def span_kind(op: str | None) -> int:
    """The OTLP span kind of an operation."""
    for pattern, kind in _KINDS:
        if op and pattern.match(op):
            return kind
    return INTERNAL


def span(record: dict[str, Any]) -> dict[str, Any]:
    """A span record (``Span.to_json()``, redacted) as an OTLP span."""
    attrs = dict(record["attributes"])
    op = record.get("op")
    if op:
        attrs["fixwire.op"] = op
    attrs["fixwire.origin"] = record.get("origin")
    flags = SAMPLED | HAS_IS_REMOTE | (IS_REMOTE if record.get("parent_remote") else 0)
    out: dict[str, Any] = {
        "traceId": record["trace_id"],
        "spanId": record["span_id"],
        "name": record["name"],
        "kind": span_kind(op),
        "startTimeUnixNano": nanos(record["start"]),
        "endTimeUnixNano": nanos(record["end"]),
        "attributes": key_values(attrs),
        "status": {"code": STATUS_ERROR if record.get("status") == "error" else STATUS_OK},
        "flags": flags,
    }
    if record.get("parent_span_id"):
        out["parentSpanId"] = record["parent_span_id"]
    return out
