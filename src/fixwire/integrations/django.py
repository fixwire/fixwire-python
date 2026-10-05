"""Django (WSGI and ASGI, sync and async views).

    MIDDLEWARE = ["fixwire.integrations.django.FixwireMiddleware", ...]

Each request gets its own isolation scope; unhandled view exceptions are
reported with the request and the URL pattern as the transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

import fixwire
from fixwire.integrations._request import pii, processor, request_info, session

if TYPE_CHECKING:
    from fixwire.types import EventProcessor, Request


class FixwireMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    def __call__(self, request: Any) -> Any:
        if self.is_async:
            return self._acall(request)
        with fixwire.isolation_scope() as iso, session():
            iso.add_event_processor(_processor(request))
            fixwire.continue_trace(dict(request.headers))
            with _server_span(request) as span:
                response = self.get_response(request)
                _finish(span, request, response)
                return response

    async def _acall(self, request: Any) -> Any:
        with fixwire.isolation_scope() as iso, session():
            iso.add_event_processor(_processor(request))
            fixwire.continue_trace(dict(request.headers))
            with _server_span(request) as span:
                response = await self.get_response(request)
                _finish(span, request, response)
                return response

    def process_exception(self, request: Any, exception: BaseException) -> None:
        if _client_error(exception):
            return None
        client = fixwire.get_client()
        if client is not None:
            client.capture_exception(exception, mechanism={"type": "django", "handled": False})
        return None


def _server_span(request: Any) -> Any:
    method = request.method or "GET"
    return fixwire.start_span(
        "%s %s" % (method, request.path_info),
        op="http.server",
        origin="auto.http.django",
        attributes={"http.request.method": method, "url.path": request.path_info},
    )


def _finish(span: Any, request: Any, response: Any) -> None:
    status = getattr(response, "status_code", 0)
    span.set_attribute("http.response.status_code", status)
    if status >= 500:
        span.set_status("error")
    match = getattr(request, "resolver_match", None)
    if match is not None and match.route:
        route = match.route if match.route.startswith("/") else "/" + match.route
        span.update_name("%s %s" % (request.method, route))
        span.set_attribute("http.route", route)


def _client_error(exception: BaseException) -> bool:
    """404, 403 and 400 responses Django makes from exceptions: not errors."""
    from django.core.exceptions import BadRequest, PermissionDenied, SuspiciousOperation
    from django.http import Http404

    return isinstance(exception, (Http404, PermissionDenied, BadRequest, SuspiciousOperation))


def _processor(request: Any) -> EventProcessor:
    def info() -> Request:
        return request_info(
            request.method or "GET",
            request.build_absolute_uri(request.path),
            request.META.get("QUERY_STRING", ""),
            request.headers.items(),
            pii(),
        )

    def transaction() -> str | None:
        match = getattr(request, "resolver_match", None)
        if match is None:
            return str(request.path_info)
        if match.route:
            route = str(match.route)
            return route if route.startswith("/") else "/" + route
        return str(match.view_name)

    return processor(info, transaction)
