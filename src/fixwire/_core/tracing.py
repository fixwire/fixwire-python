"""Tracing: spans, sampling and trace propagation.

A span started with no active span is a segment: the root of what this
process does for one request, task or job. Spans finished under a segment
are buffered with it and sent together when it ends, as one OTLP export.

Traces cross services through the W3C ``traceparent`` and ``tracestate``
headers. A continued trace keeps its caller's decision (the sampled flag); a
new one decides from its trace id's random part (its last 56 bits), as
OpenTelemetry's consistent probability sampling does, so every service of a
trace keeps or drops it alike. The caller's ``tracestate`` and ``baggage``
pass on unchanged, or not at all when too long or broken.

Nothing is registered with OpenTelemetry's globals: an app's own OTel setup
is never touched.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from collections.abc import Callable, Generator, Iterable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

from fixwire._core.event_builder import safe_str

if TYPE_CHECKING:
    from fixwire.types import SamplingContext, TracesSampler

logger = logging.getLogger("fixwire")

#: Spans kept per segment; past it they're dropped and counted.
MAX_SPANS_PER_SEGMENT = 1000
#: Attributes kept per span; past it new ones are dropped.
MAX_ATTRIBUTES = 128
#: Bytes of incoming tracestate and baggage passed on (W3C's limits); a
#: longer header is not passed on at all.
MAX_TRACESTATE = 512
MAX_BAGGAGE = 8192
#: Recorded AI content (record_ai_content): these attributes are kept to
#: MAX_AI_CONTENT bytes instead of max_value_length.
MAX_AI_CONTENT = 16_384
AI_CONTENT = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
    }
)

_TRACEPARENT = re.compile(r"[ \t]*00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})[ \t]*")
#: Control characters (but tab, which headers allow between members).
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def new_trace_id() -> str:
    return os.urandom(16).hex()


def new_span_id() -> str:
    return os.urandom(8).hex()


@dataclass
class PropagationContext:
    """The trace this request, task or job belongs to: continued from
    incoming headers, or new."""

    trace_id: str = field(default_factory=new_trace_id)
    #: The caller's span, when continued.
    parent_span_id: str | None = None
    #: The caller's sampling decision, when continued.
    sampled: bool | None = None
    #: Our own id for errors outside any span.
    span_id: str = field(default_factory=new_span_id)
    #: The caller's tracestate, passed on.
    tracestate: str = ""
    #: The caller's baggage, passed on untouched.
    baggage: str = ""
    #: Joined from incoming headers: segments in this scope belong to that
    #: trace. Otherwise each new segment starts its own trace.
    continued: bool = False

    @property
    def sample_rand(self) -> float:
        """The trace id's random part (its last 56 bits) as a fraction of
        1: a rate r keeps the trace when this is at least 1 - r."""
        return sample_rand(self.trace_id)

    @classmethod
    def from_headers(cls, headers: Mapping[str, Any]) -> PropagationContext:
        """The trace of incoming headers: continued when ``traceparent`` is
        well formed (version 00, non-zero ids); ``tracestate`` and
        ``baggage`` within their limits and without control characters."""
        h = {str(k).lower(): v for k, v in headers.items()}
        ctx = cls()
        m = _TRACEPARENT.fullmatch(_header(h, "traceparent"))
        if m and m.group(1) != "0" * 32 and m.group(2) != "0" * 16:
            ctx.continued = True
            ctx.trace_id, ctx.parent_span_id = m.group(1), m.group(2)
            ctx.sampled = bool(int(m.group(3), 16) & 1)
            ctx.tracestate = _passed_on(_header(h, "tracestate"), MAX_TRACESTATE)
        ctx.baggage = _passed_on(_header(h, "baggage"), MAX_BAGGAGE)
        return ctx


def _passed_on(value: str, limit: int) -> str:
    """An incoming header as passed on: whole, or (over ``limit`` bytes or
    with a control character) not at all."""
    if len(value.encode("utf-8", "surrogatepass")) > limit or _CONTROL.search(value):
        return ""
    return value


def _header(headers: dict[str, Any], name: str) -> str:
    """A header's value; repeated headers are joined, as W3C allows."""
    value = headers.get(name)
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in cast("list[object]", value))
    return str(value).strip(" \t") if value else ""


def _compared(url: str) -> tuple[str, str | None, int | None]:
    """A URL as trace_propagation_targets see it (without user info, query
    and fragment, its scheme and host lower case, a default port left out),
    its host and its port (or the scheme's)."""
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port
    if not parts.netloc or host is None:
        return parts.path, None, None
    default = _DEFAULT_PORTS.get(parts.scheme)
    where = "[%s]" % host if ":" in host else host
    if port is not None and port != default:
        where += ":%d" % port
    prefix = parts.scheme + "://" if parts.scheme else "//"
    return prefix + where + parts.path, host, port if port is not None else default


def _host_target(target: str) -> tuple[str, int | None]:
    """A host target's host (lower case) and port, if it names one."""
    host, sep, port = target.rpartition(":")
    if not sep or not port.isdigit() or (":" in host and not host.endswith("]")):
        host, port = target, ""  # no port, or a bare IPv6 address
    return host.strip("[]").lower(), int(port) if port else None


def propagates_to(targets: Iterable[str | re.Pattern[str]], url: str) -> bool:
    """Whether trace headers may go to ``url``: a target with "://" is a URL
    prefix; one starting with "/" a path, matching relative URLs only (a
    server sees no page origin); any other a host (and port) matching it
    and its subdomains; a regex is searched for. Each in the URL compared
    without its user info, query and fragment."""
    try:
        compared, host, port = _compared(url)
    except ValueError:  # a broken port or IPv6 address
        return False
    for t in targets:
        if isinstance(t, re.Pattern):
            if t.search(compared):
                return True
        elif not t:
            continue
        elif "://" in t:
            try:
                prefix = _compared(t)[0]
            except ValueError:
                continue
            if compared.startswith(prefix):
                return True
        elif t.startswith("/"):
            if host is None and compared.startswith(t):
                return True
        elif host is not None:
            name, wanted = _host_target(t)
            if (host == name or host.endswith("." + name)) and wanted in (None, port):
                return True
    return False


def sample_rand(trace_id: str) -> float:
    """A trace id's random part (its last 14 hex digits) as a fraction of 1,
    computed as the JavaScript SDK does, so both decide alike."""
    return int(trace_id[-14:], 16) / 2**56


def keep(trace_id: str, rate: float) -> bool:
    """Whether a rate keeps a trace (consistently across services)."""
    return sample_rand(trace_id) >= 1 - rate


def _attribute(value: Any) -> Any:
    """A span attribute: str, bool, int, float or a list of them; anything
    else as a string."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        items = cast("list[object]", value)
        return [v if isinstance(v, (str, bool, int, float)) else safe_str(v) for v in items]
    return safe_str(value)


class Span:
    """A unit of work. Use it as a context manager, or call finish()."""

    __slots__ = (
        "trace_id",
        "span_id",
        "parent_span_id",
        "name",
        "op",
        "origin",
        "status",
        "start",
        "end",
        "attributes",
        "segment",
        "sampled",
        "parent_remote",
        "_finished",
        "_token",
        "_buffer",
        "_dropped",
        "_on_segment_end",
    )

    def __init__(
        self,
        name: str,
        op: str | None,
        trace_id: str,
        parent_span_id: str | None,
        sampled: bool,
        segment: Span | None,
        origin: str = "manual",
        attributes: Mapping[str, Any] | None = None,
        *,
        on_segment_end: Callable[[Span], None] | None = None,
        parent_remote: bool = False,
    ) -> None:
        self.trace_id, self.span_id, self.parent_span_id = trace_id, new_span_id(), parent_span_id
        self.name, self.op, self.origin = name, op, origin
        self.status = "ok"
        self.start: float = time.time()
        self.end: float | None = None
        self.attributes: dict[str, Any] = {}
        self.set_attributes(attributes or {})
        self.segment = segment or self
        self.sampled = sampled
        #: The parent is in another process (the caller of a continued trace).
        self.parent_remote = parent_remote
        self._finished = False
        self._token: Token[Span | None] | None = None
        self._buffer: list[Span] = []
        self._dropped = 0
        self._on_segment_end = on_segment_end

    @property
    def is_segment(self) -> bool:
        return self.segment is self

    @property
    def dropped_spans(self) -> int:
        """Child spans not kept because the segment was full."""
        return self._dropped

    def set_attribute(self, key: str, value: Any) -> None:
        """An attribute (str, bool, int, float or a list of them; others
        become strings). None is ignored, and so are new keys once the span
        has MAX_ATTRIBUTES."""
        if value is not None and (key in self.attributes or len(self.attributes) < MAX_ATTRIBUTES):
            self.attributes[key] = value

    def set_attributes(self, values: Mapping[str, Any]) -> None:
        for k, v in values.items():
            self.set_attribute(k, v)

    def set_status(self, status: Literal["ok", "error"]) -> None:
        """Marks the span failed ("error") or not ("ok"). Spans that exit
        with an exception are marked failed already."""
        self.status = "error" if status not in ("ok", "unset") else "ok"

    def update_name(self, name: str) -> None:
        self.name = name

    def to_traceparent(self) -> str:
        return "00-%s-%s-%s" % (self.trace_id, self.span_id, "01" if self.sampled else "00")

    def finish(self, end: float | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self.end = end or time.time()
        if not self.sampled:
            return
        seg = self.segment
        if self is not seg:
            if len(seg._buffer) < MAX_SPANS_PER_SEGMENT:
                seg._buffer.append(self)
            else:
                seg._dropped += 1
            return
        if self._on_segment_end is not None:
            self._on_segment_end(self)

    def spans(self) -> list[Span]:
        """A finished segment's spans, itself first."""
        return [self, *self._buffer]

    def to_json(self) -> dict[str, Any]:
        """The span as a record, for the driver to redact and encode."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "parent_remote": self.parent_remote,
            "name": self.name,
            "op": self.op,
            "origin": self.origin,
            "status": self.status,
            "start": self.start,
            "end": self.end or time.time(),
            "attributes": {k: _attribute(v) for k, v in self.attributes.items() if v is not None},
        }

    def __enter__(self) -> Span:
        self._token = _active.set(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc is not None:
            self.set_status("error")
        if self._token is not None:
            try:
                _active.reset(self._token)
            except ValueError:  # entered in another context
                _active.set(None)
        self.finish()


_active: ContextVar[Span | None] = ContextVar("fixwire_active_span", default=None)


@contextlib.contextmanager
def use_span(span: Span | None) -> Generator[None, None, None]:
    """Makes ``span`` the active span inside the block without ending it
    (spans started inside become its children)."""
    token = _active.set(span)
    try:
        yield
    finally:
        try:
            _active.reset(token)
        except ValueError:  # left in another context
            _active.set(None)


def current_span() -> Span | None:
    return _active.get()


def sample(
    rate: float | None, sampler: TracesSampler | None, ctx: PropagationContext, name: str, attributes: Mapping[str, Any]
) -> bool:
    """Whether to record a trace. A sampler decides first (it sees the
    caller's decision as parent_sampled); then the caller's decision, when
    there is one; then traces_sample_rate. Rates apply to the trace id's
    random part, so every service of a trace decides alike."""
    if sampler is not None:
        try:
            context: SamplingContext = {"name": name, "attributes": dict(attributes), "parent_sampled": ctx.sampled}
            decision = cast("Callable[[SamplingContext], float | bool | None]", sampler)(context)
        except Exception:
            logger.exception("fixwire: traces_sampler failed; deciding without it")
            decision = None
        if decision is not None:
            return keep(ctx.trace_id, float(decision))
    if ctx.sampled is not None:
        return ctx.sampled
    return rate is not None and keep(ctx.trace_id, rate)
