"""Fixwire SDK for Python.

    import fixwire
    fixwire.init(dsn="https://<key>@<host>")   # or set FIXWIRE_DSN

The names are the familiar ones: init, capture_*, set_*, new_scope,
isolation_scope, start_span and flush. Data travels as Fixwire protocol v1:
errors, messages and spans as OTLP/HTTP JSON, the rest as small JSON bodies.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from fixwire._core.options import Options, RateLimit
from fixwire._core.scope import (
    Scope,
    get_current_scope,
    get_global_scope,
    get_isolation_scope,
    isolation_scope,
    new_scope,
)
from fixwire._core.tracing import PropagationContext, Span, current_span, use_span
from fixwire._version import __version__
from fixwire.client import AsyncClient, Client
from fixwire.types import Breadcrumb, CheckInStatus, Event, ExcInfo, Hint, Level, MonitorConfig, User

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from fixwire.types import ClientOptions

__all__ = [
    "AsyncClient",
    "Client",
    "Options",
    "RateLimit",
    "Scope",
    "__version__",
    "init",
    "get_client",
    "capture_exception",
    "capture_message",
    "capture_event",
    "capture_feedback",
    "capture_check_in",
    "set_tag",
    "set_tags",
    "set_extra",
    "set_context",
    "set_user",
    "set_level",
    "add_breadcrumb",
    "new_scope",
    "isolation_scope",
    "get_current_scope",
    "get_isolation_scope",
    "get_global_scope",
    "last_event_id",
    "flush",
    "aflush",
    "close",
    "aclose",
    "Span",
    "start_span",
    "current_span",
    "use_span",
    "continue_trace",
    "trace_headers",
    "should_propagate",
    "ai",
    "serverless_function",
]

_client: Client | None = None
_last_event_id: str | None = None


def init(dsn: str | None = None, **options: Unpack[ClientOptions]) -> Client:
    """Starts the SDK. Inside a running event loop (and with httpx
    installed) it delivers from that loop; otherwise from a thread.
    ``transport="thread"`` or ``"asyncio"`` decides explicitly."""
    global _client
    transport = options.get("transport", "auto")
    if _client is not None:
        _client.close()
    client: Client
    if transport == "asyncio" or (transport == "auto" and _loop_running() and _has_httpx()):
        client = AsyncClient(dsn, **options)
    else:
        client = Client(dsn, **options)
    for s in (get_global_scope(), get_isolation_scope()):
        s.resize(client.options.max_breadcrumbs)
    _client = client
    if client.options.default_integrations:
        from fixwire.integrations import install_defaults

        install_defaults(client)
    for integration in client.options.integrations:
        integration.setup(client)
    return client


def get_client() -> Client | None:
    return _client


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _has_httpx() -> bool:
    import importlib.util

    return importlib.util.find_spec("httpx") is not None


def _remember(event_id: str | None) -> str | None:
    global _last_event_id
    if event_id:
        _last_event_id = event_id
    return event_id


def capture_exception(error: BaseException | ExcInfo | None = None) -> str | None:
    """Reports an exception (default: the one being handled)."""
    return _remember(_client.capture_exception(error)) if _client else None


def capture_message(message: str, level: Level = "info") -> str | None:
    """Reports a message. Returns the event id, or None when it was dropped."""
    return _remember(_client.capture_message(message, level)) if _client else None


def capture_feedback(
    message: str | None = None,
    *,
    score: float | None = None,
    trace_id: str | None = None,
    event_id: str | None = None,
    url: str | None = None,
    source: str = "api",
) -> str | None:
    """Sends feedback: how someone rated an AI answer (``score`` from -1 to 1
    with the ``trace_id`` that produced it; default: the current trace)
    and/or what they said about a crash (``event_id``, e.g.
    ``last_event_id()``). A negative rating opens a user_feedback issue for
    the agent. Returns the feedback's id, or None when there was nothing to
    send::

        fixwire.capture_feedback("Refunded the wrong order", score=-1, trace_id=run_trace_id)
    """
    if not _client:
        return None
    return _client.capture_feedback(message, score=score, trace_id=trace_id, event_id=event_id, url=url, source=source)


def capture_check_in(
    monitor: str,
    status: CheckInStatus = "ok",
    *,
    check_in_id: str | None = None,
    duration: float | None = None,
    monitor_config: MonitorConfig | None = None,
) -> str | None:
    """Reports a run of a scheduled job to its monitor (its slug):
    ``"in_progress"`` when it starts, then ``"ok"`` or ``"error"`` with the
    returned id and the run's duration in seconds::

        run = fixwire.capture_check_in("nightly-report", "in_progress")
        ...
        fixwire.capture_check_in("nightly-report", "ok", check_in_id=run, duration=42.5)
    """
    if not _client:
        return None
    return _client.capture_check_in(
        monitor, status, check_in_id=check_in_id, duration=duration, monitor_config=monitor_config
    )


def capture_event(event: Event | dict[str, Any], hint: Hint | None = None) -> str | None:
    """Sends an event you built (shaped like ``fixwire.types.Event``)."""
    return _remember(_client.capture_event(event, hint)) if _client else None


def last_event_id() -> str | None:
    return _last_event_id


def set_tag(key: str, value: object) -> None:
    """A searchable key/value on events of this request or job."""
    get_isolation_scope().set_tag(key, value)


def set_tags(tags: Mapping[str, object]) -> None:
    for k, v in tags.items():
        set_tag(k, v)


def set_extra(key: str, value: object) -> None:
    get_isolation_scope().set_extra(key, value)


def set_context(key: str, value: dict[str, Any] | None) -> None:
    """Structured data under ``contexts[key]``; None removes it."""
    get_isolation_scope().set_context(key, value)


def set_user(user: User | None) -> None:
    """Who hit the error: ``set_user({"id": 42, "email": "a@example.com"})``. None clears it."""
    get_isolation_scope().set_user(user)


def set_level(level: Level) -> None:
    get_isolation_scope().set_level(level)


def add_breadcrumb(
    crumb: Breadcrumb | None = None, hint: dict[str, Any] | None = None, **fields: Unpack[Breadcrumb]
) -> None:
    """Records a step on the way to an error: ``add_breadcrumb(category="cart",
    message="added sku-1")``. Events captured later in this request or job
    carry the latest breadcrumbs."""
    merged: Breadcrumb = {**(crumb or {}), **fields}
    merged.setdefault("timestamp", time.time())
    before = _client.options.before_breadcrumb if _client else None
    if before is not None:
        try:
            changed = cast("Callable[[Breadcrumb, dict[str, Any]], Breadcrumb | None]", before)(merged, hint or {})
        except Exception:
            changed = merged
        if changed is None:
            return
        merged = changed
    get_isolation_scope().add_breadcrumb(merged)


def flush(timeout: float | None = None) -> bool:
    return _client.flush(timeout) if _client else True


async def aflush(timeout: float | None = None) -> bool:
    return await _client.aflush(timeout) if _client else True


def close(timeout: float | None = None) -> None:
    global _client
    if _client:
        _client.close(timeout)
        _client = None


async def aclose(timeout: float | None = None) -> None:
    global _client
    if _client:
        await _client.aclose(timeout)
        _client = None


# Tracing.


def start_span(
    name: str, op: str | None = None, attributes: Mapping[str, Any] | None = None, origin: str = "manual"
) -> Span:
    """A span, used as a context manager::

        with fixwire.start_span("charge card", op="payment"):
            ...

    With no active span it starts a segment (the root for this process),
    sampled per traces_sample_rate; nested spans join the active one."""
    parent = current_span()
    if parent is not None:
        return Span(name, op, parent.trace_id, parent.span_id, parent.sampled, parent.segment, origin, attributes)
    from fixwire._core.scope import propagation_context
    from fixwire._core.tracing import sample

    ctx = propagation_context()
    if not ctx.continued:
        # A local root: its own trace and its own sampling decision. Errors
        # outside it keep the scope's trace id.
        ctx = PropagationContext()
    client = _client
    sampled = False
    if client is not None and client.enabled:
        o = client.options
        sampled = sample(o.traces_sample_rate, o.traces_sampler, ctx, name, attributes or {})
    return Span(
        name,
        op,
        ctx.trace_id,
        ctx.parent_span_id,
        sampled,
        None,
        origin,
        attributes,
        on_segment_end=client.capture_segment if sampled and client is not None else None,
        parent_remote=ctx.parent_span_id is not None,
    )


def continue_trace(headers: Mapping[str, Any]) -> PropagationContext:
    """Joins the trace of incoming headers (W3C ``traceparent`` and
    ``tracestate``; ``baggage`` passes on) for the current isolation scope.
    Integrations call it per request; call it yourself for queues or custom
    protocols."""
    ctx = PropagationContext.from_headers(headers)
    get_isolation_scope().propagation = ctx
    return ctx


def trace_headers(baggage: str = "", span: Span | None = None) -> dict[str, str]:
    """Headers that carry the current trace to another service:
    ``traceparent``; the caller's ``tracestate`` when this trace continues
    theirs; and ``baggage``, the outgoing request's own (``baggage``) then
    the caller's. The receiver continues from ``span`` (default: the active
    span)."""
    from fixwire._core.scope import propagation_context

    span = span or current_span()
    ctx = propagation_context()
    if span is not None:
        trace_id, span_id, sampled = span.trace_id, span.span_id, span.sampled
    else:
        trace_id, span_id, sampled = ctx.trace_id, ctx.span_id, bool(ctx.sampled)
    out = {"traceparent": "00-%s-%s-%s" % (trace_id, span_id, "01" if sampled else "00")}
    if ctx.tracestate and ctx.trace_id == trace_id:
        out["tracestate"] = ctx.tracestate
    merged = _merge_baggage(baggage, ctx.baggage)
    if merged:
        out["baggage"] = merged
    return out


def _merge_baggage(own: str, incoming: str) -> str:
    """A request's own baggage, then the incoming members it doesn't set."""
    members = [m.strip() for m in (own or "").split(",") if m.strip()]
    keys = {m.split("=", 1)[0].strip() for m in members}
    for m in (incoming or "").split(","):
        m = m.strip()
        if m and m.split("=", 1)[0].strip() not in keys:
            members.append(m)
    return ",".join(members)


def should_propagate(url: str) -> bool:
    """Whether trace headers may go to url (trace_propagation_targets)."""
    import re as _re

    targets = _client.options.trace_propagation_targets if _client is not None else []
    for t in targets:
        if isinstance(t, _re.Pattern):
            if t.search(url):
                return True
        elif t in url:
            return True
    return False


# AI agent tracing (fixwire.ai.agent, .chat, .tool, wrap_anthropic, wrap_openai): last, as it uses the API above.
from fixwire import ai  # noqa: E402

# Serverless handlers (fixwire.serverless_function): last too, for the same reason.
from fixwire.serverless import serverless_function  # noqa: E402
