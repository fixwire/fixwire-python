"""The event pipeline, split so the caller's thread or event loop only does
cheap work:

  caller:  build → scopes → fingerprint → budgets ─► drop
                 → sample_rate ─► drop → processors → before_send → queue
  driver:  source context → serialize → REDACT → cut strings → OTLP log
           record → size guard → gzip → a /v1/logs request

Dropped events never pay for serialization or redaction, and redaction runs
after before_send, so nothing a callback adds escapes it. It reads each
string's part kept and the next 16 kB before the cut to max_value_length, so
a secret the cut goes through is still found. Spans, sessions, check-ins and
feedback are queued the same way and become their own requests
(sdks/PROTOCOL.md).
"""

from __future__ import annotations

import logging
import platform
import random
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from fixwire._core import event_builder, protocol, scope
from fixwire._core.delivery import Outbound
from fixwire._core.dsn import SDK_NAME, Dsn
from fixwire._core.jsonish import get_dict
from fixwire._core.limiter import Limiter, fingerprint
from fixwire._core.options import Options
from fixwire._core.redact import Redactor
from fixwire._core.serializer import Serializer, clip, clip_strings, window
from fixwire._core.sessions import MAX_AGGREGATES, Aggregates
from fixwire._core.tracing import AI_CONTENT, MAX_AI_CONTENT
from fixwire._version import __version__

if TYPE_CHECKING:
    from fixwire._core.tracing import Span
    from fixwire.types import EventProcessor, ExcInfo, Hint, Mechanism


def call_processor(fn: EventProcessor, event: dict[str, Any], hint: Hint) -> dict[str, Any] | None:
    """Calls an event processor or before_send, typed either way."""
    plain = cast("Callable[[dict[str, Any], Hint], dict[str, Any] | None]", fn)
    return plain(event, hint)


logger = logging.getLogger("fixwire")

#: The ingest's limit for one error or message.
MAX_EVENT_BYTES = 1 << 20
#: The ingest's limits for one request of spans.
MAX_SPANS_BYTES = 5 << 20
MAX_SPANS_PER_REQUEST = 100

#: Marks a queued item that isn't an event: "spans", "sessions",
#: "feedback" or "check_in".
_KIND = "__kind__"

#: Fields redaction skips: ids, times and the SDK's own.
_REDACT_SKIP = (
    "event_id",
    "timestamp",
    "platform",
    "level",
    "sdk",
    "release",
    "dist",
    "environment",
    "start_timestamp",
)
_FEEDBACK_SKIP = ("sdk", "feedback_id", "timestamp", "event_id", "trace_id", "release", "environment", "source")


class EventQueue:
    """Thread-safe and never blocks: past max_size new events are dropped."""

    def __init__(self, max_size: int) -> None:
        self._items: deque[dict[str, Any]] = deque()
        self._max = max_size
        self._lock = threading.Lock()
        self.overflowed = 0

    def put(self, event: dict[str, Any]) -> bool:
        """Queues an event; False when the queue is full and it is dropped."""
        with self._lock:
            if len(self._items) >= self._max:
                self.overflowed += 1
                return False
            self._items.append(event)
            return True

    def drain(self) -> list[Any]:
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class Core:
    def __init__(self, options: Options) -> None:
        self.options = options
        self.dsn: Dsn | None = Dsn.parse(options.dsn) if options.dsn else None
        self.builder = event_builder.Options(
            include_local_variables=options.include_local_variables,
            max_value_length=options.max_value_length,
            max_stack_frames=options.max_stack_frames,
            in_app_include=options.in_app_include,
            in_app_exclude=options.in_app_exclude,
            project_root=options.project_root,
        )
        # Strings are serialized to what redaction reads, and cut once redacted.
        self.serializer = Serializer(window(options.max_value_length))
        self.ai_serializer = Serializer(window(MAX_AI_CONTENT))
        self.redactor = Redactor(sensitive_keys=options.sensitive_keys) if options.redact else None
        rl = options.rate_limit
        self.limiter = Limiter(rl.per_issue_burst, rl.per_issue_per_minute, rl.global_per_minute, rl.enabled)
        self.queue = EventQueue(options.max_queue_size)
        #: Request sessions, until sent (release health).
        self.sessions = Aggregates()
        #: The resource's service.name.
        self.service = protocol.service_name(options.release)
        self._random = random.random
        self.spool = None
        if options.offline and options.dsn:
            from fixwire.drivers.spool import open_spool

            try:
                self.spool = open_spool(options.offline, options.dsn)
            except Exception:
                logger.exception("fixwire: could not open the offline spool; continuing without it")

    def after_fork(self) -> None:
        """In a forked child: new locks (one held by another thread at the
        fork would stay held) and an empty queue (the parent sends it)."""
        self.queue = EventQueue(self.options.max_queue_size)
        self.limiter.after_fork()
        self.sessions.after_fork()

    # Caller side.

    def event_from_exception(
        self, error: BaseException | ExcInfo | None = None, mechanism: Mechanism | None = None
    ) -> tuple[dict[str, Any], Hint]:
        exc_info = event_builder.exc_info_from_error(error)
        values = event_builder.exceptions_from_error_tuple(exc_info, self.builder, mechanism)
        handled = (mechanism or {}).get("handled", True)
        return {"level": "error" if handled else "fatal", "exception": {"values": values}}, {"exc_info": exc_info}

    def event_from_message(self, message: str, level: str = "info", stacktrace: bool = False) -> dict[str, Any]:
        event: dict[str, Any] = {"level": level, "message": message}
        if stacktrace:
            event["threads"] = {
                "values": [{"stacktrace": event_builder.current_stacktrace(self.builder), "current": True}]
            }
        return event

    def feedback_item(
        self, message: str, score: float, trace_id: str | None, event_id: str | None, url: str | None, source: str
    ) -> dict[str, Any]:
        """A /v1/feedback body for the queue: neither sampled, rate limited
        nor passed to before_send (redaction still applies when encoded)."""
        o = self.options
        if trace_id is None:
            from fixwire._core.tracing import current_span

            span = current_span()
            trace_id = span.trace_id if span is not None else scope.propagation_context().trace_id
        body: dict[str, Any] = {
            "sdk": protocol.sdk(),
            "feedback_id": uuid.uuid4().hex,
            "timestamp": time.time(),
            "trace_id": trace_id,
            "source": source,
            "environment": o.environment,
        }
        if message:
            body["message"] = message
        if score:
            body["score"] = score
        if event_id:
            body["event_id"] = event_id
        if url:
            body["url"] = url
        if o.release:
            body["release"] = o.release
        # The person who gave it, as the scopes know them.
        user: dict[str, Any] = {}
        for s in (scope.get_global_scope(), scope.get_isolation_scope(), scope.get_current_scope()):
            if s.user:
                user.update(s.user)
        if user.get("username"):
            body["name"] = str(user["username"])
        if user.get("email"):
            body["email"] = str(user["email"])
        return {_KIND: "feedback", "body": body}

    @staticmethod
    def sessions_item() -> dict[str, Any]:
        """Sends the counted request sessions when the driver gets to it."""
        return {_KIND: "sessions"}

    def check_in_item(
        self,
        monitor: str,
        status: str,
        check_in_id: str,
        duration: float | None,
        monitor_config: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """A /v1/check-ins/{monitor} body for the queue."""
        body: dict[str, Any] = {
            "sdk": protocol.sdk(),
            "check_in_id": check_in_id,
            "status": status,
            "environment": self.options.environment,
        }
        if duration is not None:
            body["duration"] = duration
        if monitor_config:
            body["monitor_config"] = monitor_config
        return {_KIND: "check_in", "monitor": monitor, "body": body}

    def prepare(self, event: dict[str, Any], hint: Hint | None = None) -> dict[str, Any] | None:
        """Runs the caller-side pipeline; None when the event is dropped."""
        hint = hint or {}
        o = self.options
        event.setdefault("event_id", uuid.uuid4().hex)
        event.setdefault("timestamp", time.time())
        event.setdefault("platform", "python")
        event.setdefault("level", "error")
        for key in ("release", "environment", "dist", "server_name"):
            value = getattr(o, key)
            if value and key not in event:
                event[key] = value
        event["sdk"] = {
            "name": SDK_NAME,
            "version": __version__,
            "packages": [{"name": "pypi:fixwire", "version": __version__}],
        }
        processors = scope.apply(event, o.max_breadcrumbs)
        contexts = event.setdefault("contexts", {})
        if "trace" not in contexts:
            # Errors link to the trace (and span) they happened in.
            from fixwire._core.tracing import current_span

            span = current_span()
            if span is not None:
                contexts["trace"] = {"trace_id": span.trace_id, "span_id": span.span_id, "op": span.op or "default"}
                if span.parent_span_id:
                    contexts["trace"]["parent_span_id"] = span.parent_span_id
            else:
                ctx = scope.propagation_context()
                contexts["trace"] = {"trace_id": ctx.trace_id, "span_id": ctx.span_id}
                if ctx.parent_span_id:
                    contexts["trace"]["parent_span_id"] = ctx.parent_span_id
        contexts.setdefault("runtime", {"name": platform.python_implementation(), "version": platform.python_version()})

        fp = fingerprint(event)
        allowed, suppressed = self.limiter.allow(fp, time.time())
        if not allowed:
            self._drop("ratelimit_backoff")
            return None
        if o.sample_rate < 1.0 and self._random() >= o.sample_rate:
            self._drop("sample_rate")
            return None
        for proc in processors:
            try:
                processed = call_processor(proc, event, hint)
            except Exception:
                logger.exception("fixwire: an event processor failed")
                continue
            if processed is None:
                self._drop("event_processor")
                return None
            event = processed
        if "transaction" not in event:
            # No integration named it: the segment's name, if in one.
            from fixwire._core.tracing import current_span as _current

            span = _current()
            if span is not None:
                event["transaction"] = span.segment.name
        if o.before_send is not None:
            sent: dict[str, Any] | None = event
            try:
                sent = call_processor(o.before_send, event, hint)
            except Exception:
                logger.exception("fixwire: before_send failed; sending the event unchanged")
            if sent is None:
                self._drop("before_send")
                return None
            event = sent
        if suppressed:
            event.setdefault("contexts", {}).setdefault("fixwire", {})["suppressed"] = suppressed
        return event

    def _drop(self, reason: str) -> None:
        if self.options.debug:
            logger.info("fixwire: dropped an event (%s)", reason)

    # Driver side.

    def sessions_on(self) -> bool:
        return self.dsn is not None and self.options.auto_session_tracking and bool(self.options.release)

    def mark_session(self, event: dict[str, Any]) -> None:
        """An error marks the request's session errored, or crashed when
        nothing handled it."""
        rs = scope.get_isolation_scope().request_session
        if rs is None:
            return
        exception = cast("dict[str, Any]", event.get("exception") or {})
        values = cast("list[dict[str, Any]]", exception.get("values") or [])
        if not values and event.get("level") not in ("error", "fatal"):
            return
        if any(cast("dict[str, Any]", v.get("mechanism") or {}).get("handled") is False for v in values):
            rs.status = "crashed"
        elif rs.status == "ok":
            rs.status = "errored"

    def encode(self, item: dict[str, Any]) -> list[Outbound]:
        """Serializes, redacts and compresses a queued item (an event, a
        segment's spans, the request sessions, a check-in or feedback) into
        the requests that carry it."""
        kind = item.get(_KIND)
        try:
            if kind == "spans":
                return list(self._encode_spans(item["spans"]))
            if kind == "sessions":
                return self._encode_sessions()
            if kind == "feedback":
                body = cast("dict[str, Any]", item["body"])
                return [
                    self._request(protocol.FEEDBACK, protocol.dumps(self._redact(body, _FEEDBACK_SKIP)), "feedback")
                ]
            if kind == "check_in":
                path = protocol.CHECK_INS + quote(str(item["monitor"]), safe="")
                return [self._request(path, protocol.dumps(item["body"]), "check_in")]
            out = self._encode_event(item)
            return [out] if out is not None else []
        except Exception:
            logger.exception("fixwire: could not encode %s", kind or "an event")
            return []

    def _request(self, path: str, body: bytes, category: str) -> Outbound:
        return Outbound(path, "application/json", protocol.compress(body), category)

    def _redact(self, data: dict[str, Any], skip: tuple[str, ...]) -> dict[str, Any]:
        if self.redactor is None:
            return data
        skipped = {k: data.pop(k) for k in skip if k in data}
        data, _ = self.redactor.walk(data)
        data.update(skipped)
        return data

    def _encode_event(self, event: dict[str, Any]) -> Outbound | None:
        if self.options.include_source_context:
            event_builder.add_source_context(event, self.options.max_value_length)
        # Each field is a value of its own for the serializer's limits.
        data = {str(k): self.serializer(v, 1) for k, v in event.items()}
        request = get_dict(data.get("request"))
        if request.get("url") and request.get("query_string"):
            # The query is redacted as part of its URL, as the server reads it ("?code=…").
            request["url"] = "%s?%s" % (request["url"], request.pop("query_string"))
        data = clip_strings(self._redact(data, _REDACT_SKIP), self.options.max_value_length)
        res = protocol.resource(
            self.service,
            data.get("release") or None,
            data.get("environment") or None,
            data.get("server_name") or None,
        )

        def encode(d: dict[str, Any]) -> bytes:
            return protocol.dumps(protocol.logs(res, [protocol.log_record(d)]))

        body = encode(data)
        if len(body) > MAX_EVENT_BYTES:
            shrunk = self._shrink(data, encode)
            if shrunk is None:
                self._drop("too_large")
                return None
            body = shrunk
        return self._request(protocol.LOGS, body, "error")

    def segment_payload(self, segment: Span) -> dict[str, Any]:
        """A finished segment as a queue item; encoded by the driver."""
        return {_KIND: "spans", "spans": [s.to_json() for s in segment.spans()]}

    def _string(self, s: str, limit: int) -> str:
        """A string as sent: redacted over the part kept and the next 16 kB,
        then cut to ``limit``."""
        s = clip(s, window(limit))
        if self.redactor is not None:
            s, _ = self.redactor.mask_or_filter(s)
        return clip(s, limit)

    def _bound_span(self, record: dict[str, Any]) -> None:
        """A span's strings as an event's (redacted, then cut); recorded AI
        content keeps MAX_AI_CONTENT."""
        limit = self.options.max_value_length
        record["name"] = self._string(record["name"], limit)
        for key in ("op", "origin"):
            if isinstance(record.get(key), str):
                record[key] = self._string(record[key], limit)
        attrs = {
            k: (self.ai_serializer if k in AI_CONTENT else self.serializer)(v, 1)
            for k, v in record["attributes"].items()
        }
        if self.redactor is not None:
            # Attribute values (URLs, queries, messages) and names.
            attrs, _ = self.redactor.walk(attrs)
        record["attributes"] = {
            clip(k, limit): clip_strings(v, MAX_AI_CONTENT if k in AI_CONTENT else limit) for k, v in attrs.items()
        }

    def _encode_spans(self, records: list[dict[str, Any]]) -> Iterator[Outbound]:
        for s in records:
            self._bound_span(s)
        o = self.options
        res = protocol.resource(self.service, o.release, o.environment, o.server_name)
        spans = [protocol.span(s) for s in records]
        if len(spans) <= MAX_SPANS_PER_REQUEST:
            body = protocol.dumps(protocol.traces(res, spans))
            if len(body) <= MAX_SPANS_BYTES:
                yield self._request(protocol.TRACES, body, "span")
                return
        # Too much for one request: in batches within the limits; a span
        # that can't fit in one alone is dropped alone.
        room = MAX_SPANS_BYTES - len(protocol.dumps(protocol.traces(res, [])))
        batch: list[dict[str, Any]] = []
        size = 0
        for s in spans:
            n = len(protocol.dumps(s)) + 1  # and a comma
            if n > room:
                self._drop("span too large")
                continue
            if batch and (len(batch) >= MAX_SPANS_PER_REQUEST or size + n > room):
                yield self._request(protocol.TRACES, protocol.dumps(protocol.traces(res, batch)), "span")
                batch, size = [], 0
            batch.append(s)
            size += n
        if batch:
            yield self._request(protocol.TRACES, protocol.dumps(protocol.traces(res, batch)), "span")

    def _encode_sessions(self) -> list[Outbound]:
        aggregates = self.sessions.take()
        if aggregates is None:
            return []
        out: list[Outbound] = []
        for i in range(0, len(aggregates), MAX_AGGREGATES):
            body = {
                "sdk": protocol.sdk(),
                "release": self.options.release or "",
                "environment": self.options.environment or "production",
                "aggregates": aggregates[i : i + MAX_AGGREGATES],
            }
            out.append(self._request(protocol.SESSIONS, protocol.dumps(body), "session"))
        return out

    @staticmethod
    def _shrink(data: dict[str, Any], encode: Callable[[dict[str, Any]], bytes]) -> bytes | None:
        """Leaves out the breadcrumbs, then the frames' local variables,
        then the contexts (the trace stays), until the record fits; None
        when it never does."""

        def drop_vars() -> None:
            for frame in event_builder.iter_event_frames(data):
                frame.pop("vars", None)

        def drop_contexts() -> None:
            contexts = data.get("contexts")
            if isinstance(contexts, dict):
                kept = cast("dict[str, Any]", contexts)
                data["contexts"] = {k: kept[k] for k in ("trace", "fixwire") if k in kept}

        for shed in (lambda: data.pop("breadcrumbs", None), drop_vars, drop_contexts):
            shed()
            body = encode(data)
            if len(body) <= MAX_EVENT_BYTES:
                return body
        return None

    # The offline spool. Called from the driver only.

    def spool_put(self, out: Outbound) -> None:
        if self.spool is not None and out.spool_id is None:
            try:
                out.spool_id = self.spool.put(out)
            except Exception:
                logger.debug("fixwire: spool write failed", exc_info=True)

    def spool_done(self, out: Outbound) -> None:
        if self.spool is not None and out.spool_id is not None:
            try:
                self.spool.delete(out.spool_id)
            except Exception:
                logger.debug("fixwire: spool delete failed", exc_info=True)

    def spool_load(self) -> list[Outbound]:
        if self.spool is None:
            return []
        try:
            self.spool.touch()
            return self.spool.claim()
        except Exception:
            logger.debug("fixwire: spool read failed", exc_info=True)
            return []

    def url(self, out: Outbound) -> str:
        assert self.dsn is not None
        return self.dsn.url(out.path)

    def headers(self, out: Outbound) -> dict[str, str]:
        assert self.dsn is not None
        return {
            "Content-Type": out.content_type,
            "Content-Encoding": "gzip",
            "Authorization": self.dsn.auth_header(),
            "User-Agent": "%s/%s" % (SDK_NAME, __version__),
        }
