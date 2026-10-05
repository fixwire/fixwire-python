"""Outgoing HTTP requests, shared by the client integrations: a child span
of the active span (when it is sampled), trace headers for
trace_propagation_targets continuing from that span, and an "http"
breadcrumb. Nothing here raises into the caller's request."""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import TYPE_CHECKING

import fixwire
from fixwire.transport import in_sdk_request

if TYPE_CHECKING:
    from fixwire._core.tracing import Span

logger = logging.getLogger("fixwire")


class Outgoing:
    """One request in flight."""

    __slots__ = ("method", "url", "span")

    def __init__(self, method: str, url: str, span: Span | None) -> None:
        self.method, self.url, self.span = method, url, span

    def end(self, status: int | None = None, error: BaseException | None = None) -> None:
        """Ends the span (failed on an error or a 4xx/5xx) and records the breadcrumb."""
        try:
            failed = error is not None or (status is not None and status >= 400)
            if self.span is not None:
                self.span.set_attribute("http.response.status_code", status)
                if failed:
                    self.span.set_status("error")
                    if error is not None:
                        self.span.set_attribute("error.type", type(error).__name__)
                self.span.finish()
            data: dict[str, object] = {"method": self.method, "url": self.url}
            if status is not None:
                data["status_code"] = status
            serious = error is not None or (status or 0) >= 500
            fixwire.add_breadcrumb(type="http", category="http", level="error" if serious else "info", data=data)
        except Exception:
            logger.debug("fixwire: could not record an outgoing request", exc_info=True)


def begin(method: str, url: str, host: str | None, headers: MutableMapping[str, str], origin: str) -> Outgoing | None:
    """Starts tracking a request (None for the SDK's own); adds trace headers
    to ``headers`` when the URL is a propagation target."""
    if in_sdk_request.get():
        return None
    try:
        plain = url.split("?", 1)[0].split("#", 1)[0]
        method = method.upper()
        span: Span | None = None
        parent = fixwire.current_span()
        if parent is not None and parent.sampled:
            span = fixwire.start_span(
                "%s %s" % (method, plain),
                op="http.client",
                origin=origin,
                attributes={"http.request.method": method, "url.full": plain, "server.address": host},
            )
        if fixwire.should_propagate(url) and not any(k.lower() == "traceparent" for k in headers):
            existing = next((v for k, v in headers.items() if k.lower() == "baggage"), "")
            for k, v in fixwire.trace_headers(existing, span=span).items():
                headers[k] = v
        return Outgoing(method, plain, span)
    except Exception:
        logger.debug("fixwire: could not trace an outgoing request", exc_info=True)
        return None
