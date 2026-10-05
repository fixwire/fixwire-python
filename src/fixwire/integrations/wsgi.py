"""WSGI: Flask, Bottle, Pyramid, Falcon (anything WSGI).

    from fixwire.integrations.wsgi import FixwireMiddleware
    app = FixwireMiddleware(app)

For Flask, ``fixwire.integrations.flask.init_app(app)`` adds this and the
error reports Flask keeps to itself.

Each request gets its own isolation scope and continues the caller's trace;
with tracing on it is a segment named after its route (Werkzeug's URL rule
when there is one). The scope and the span last until the server closes the
response, so streamed responses are covered too. Exceptions that escape the
app are reported with the request.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any

import fixwire
from fixwire.integrations._request import pii, processor, request_info, session

if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment

    from fixwire.types import Request


class FixwireMiddleware:
    def __init__(self, app: WSGIApplication) -> None:
        self.app = app

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        # The request runs in a context of its own (the app call, the body and
        # the close), so nothing leaks into the server's context, even when a
        # response is never read or closed.
        ctx = contextvars.copy_context()
        return ctx.run(self._call, ctx, environ, start_response)

    def _call(
        self, ctx: contextvars.Context, environ: WSGIEnvironment, start_response: StartResponse
    ) -> Iterable[bytes]:
        stack = contextlib.ExitStack()
        iso = stack.enter_context(fixwire.isolation_scope())
        stack.enter_context(session())
        iso.add_event_processor(processor(lambda: _request(environ), lambda: route(environ)))
        fixwire.continue_trace(_headers(environ))
        method = str(environ.get("REQUEST_METHOD") or "GET")
        path = str(environ.get("PATH_INFO") or "/")
        span = stack.enter_context(
            fixwire.start_span(
                "%s %s" % (method, path),
                op="http.server",
                origin="auto.http.wsgi",
                attributes={
                    "http.request.method": method,
                    "url.path": path,
                    "url.scheme": environ.get("wsgi.url_scheme"),
                },
            )
        )
        started = finished = False

        def finish() -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            name = route(environ)
            if name and name != path:
                span.update_name("%s %s" % (method, name))
                span.set_attribute("http.route", name)
            stack.close()

        def traced_start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            nonlocal started
            started = True
            code = int(status.split(" ", 1)[0] or 0)
            span.set_attribute("http.response.status_code", code)
            if code >= 500:
                span.set_status("error")
            return start_response(status, headers, exc_info)

        try:
            result = self.app(environ, traced_start_response)
        except Exception as e:
            if not started:
                span.set_attribute("http.response.status_code", 500)
            span.set_status("error")
            _report(e)
            finish()
            raise
        return _Response(result, ctx, finish)


class _Response:
    """The app's response, produced in the request's context: errors while
    iterating are reported, and the request's scope and span end once it's
    fully sent or closed."""

    def __init__(self, result: Iterable[bytes], ctx: contextvars.Context, finish: Callable[[], None]) -> None:
        self._result = result
        self._ctx = ctx
        self._finish = finish

    def __iter__(self) -> Iterator[bytes]:
        it = self._ctx.run(iter, self._result)
        while True:
            try:
                chunk = self._ctx.run(next, it, None)
            except Exception as e:
                self._ctx.run(_report, e)
                raise
            if chunk is None:
                break
            yield chunk
        self._ctx.run(self._finish)

    def close(self) -> None:
        try:
            close = getattr(self._result, "close", None)
            if callable(close):
                self._ctx.run(close)
        finally:
            self._ctx.run(self._finish)


def _report(error: BaseException) -> None:
    client = fixwire.get_client()
    if client is not None:
        client.capture_exception(error, mechanism={"type": "wsgi", "handled": False})


def _headers(environ: WSGIEnvironment) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in environ.items():
        if k.startswith("HTTP_"):
            out[k[5:].replace("_", "-").lower()] = str(v)
    return out


def _request(environ: WSGIEnvironment) -> Request:
    headers = list(_headers(environ).items())
    if environ.get("CONTENT_TYPE"):
        headers.append(("content-type", str(environ["CONTENT_TYPE"])))
    scheme = str(environ.get("wsgi.url_scheme") or "http")
    host = str(environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "")
    url = "%s://%s%s%s" % (scheme, host, environ.get("SCRIPT_NAME") or "", environ.get("PATH_INFO") or "")
    return request_info(
        str(environ.get("REQUEST_METHOD") or "GET"), url, str(environ.get("QUERY_STRING") or ""), headers, pii()
    )


#: The environ key a framework (or your code) sets to the request's route pattern.
ROUTE_KEY = "fixwire.route"


def route(environ: WSGIEnvironment) -> str | None:
    """The request's route pattern: ``environ["fixwire.route"]`` when set (the
    Flask integration sets it), else Werkzeug's URL rule while the request is
    active, else the path."""
    known = environ.get(ROUTE_KEY)
    if isinstance(known, str):
        return known
    request = environ.get("werkzeug.request")
    rule = getattr(getattr(request, "url_rule", None), "rule", None)
    if isinstance(rule, str):
        return rule
    path = environ.get("PATH_INFO")
    return str(path) if path else None
