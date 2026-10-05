"""Celery: each task runs in its own isolation scope tagged with the task,
failures are reported (not retries or ignored tasks), and worker processes
flush before they exit.

    fixwire.init(dsn=..., integrations=[CeleryIntegration()])
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import fixwire
from fixwire._core.jsonish import as_dict, get_dict
from fixwire.integrations import install_once

if TYPE_CHECKING:
    from fixwire.client import Client
    from fixwire.types import Event, Hint

_scopes: dict[str, Any] = {}


class CeleryIntegration:
    def setup(self, client: Client) -> None:
        if not install_once("celery"):
            return
        from celery import signals

        signals.before_task_publish.connect(_publish, weak=False)
        signals.task_prerun.connect(_prerun, weak=False)
        signals.task_postrun.connect(_postrun, weak=False)
        signals.task_failure.connect(_failure, weak=False)
        signals.worker_process_shutdown.connect(_shutdown, weak=False)


def _publish(headers: Any = None, **_: Any) -> None:
    """Producers pass the trace on in the message headers."""
    out = as_dict(headers)
    if out is not None:
        for k, v in fixwire.trace_headers().items():
            out.setdefault(k, v)


def _prerun(sender: Any = None, task_id: str = "", task: Any = None, **_: Any) -> None:
    cm = fixwire.isolation_scope()
    iso = cm.__enter__()
    name = str(getattr(task, "name", None) or getattr(sender, "name", "") or "")
    request = getattr(task, "request", None)
    fixwire.continue_trace(_incoming(request))
    route = get_dict(getattr(request, "delivery_info", None)).get("routing_key")
    span = fixwire.start_span(
        name,
        op="queue.process",
        origin="auto.queue.celery",
        attributes={"messaging.system": "celery", "messaging.message.id": task_id, "messaging.destination.name": route},
    )
    span.__enter__()
    _scopes[task_id] = (cm, span)
    iso.set_tag("celery_task", name)
    iso.set_context("celery", {"task_id": task_id, "task": name, "retries": getattr(request, "retries", 0)})

    def transaction(event: Event, hint: Hint) -> Event:
        if name:
            event.setdefault("transaction", name)
        return event

    iso.add_event_processor(transaction)


def _incoming(request: Any) -> dict[str, str]:
    """Trace headers a producer put on the message."""
    out: dict[str, str] = {}
    if request is None:
        return out
    headers = get_dict(getattr(request, "headers", None))
    for k in ("traceparent", "tracestate", "baggage"):
        v: Any = (request.get(k) if hasattr(request, "get") else None) or headers.get(k)
        if v:
            out[k] = str(v)
    return out


def _postrun(task_id: str = "", state: Any = None, **_: Any) -> None:
    entry = _scopes.pop(task_id, None)
    if entry is not None:
        cm, span = entry
        if state == "FAILURE":
            span.set_status("error")
        span.__exit__(None, None, None)
        cm.__exit__(None, None, None)


def _failure(
    sender: Any = None, task_id: str = "", exception: BaseException | None = None, traceback: Any = None, **_: Any
) -> None:
    if exception is None:
        return
    try:
        from celery.exceptions import Ignore, Reject, Retry

        if isinstance(exception, (Ignore, Reject, Retry)):
            return
    except ImportError:  # pragma: no cover
        pass
    client = fixwire.get_client()
    if client is not None:
        client.capture_exception(
            (type(exception), exception, traceback), mechanism={"type": "celery", "handled": False}
        )


def _shutdown(**_: Any) -> None:
    fixwire.flush()
