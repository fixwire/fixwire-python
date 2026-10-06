"""ASGI: Starlette, FastAPI, Litestar, Quart (anything ASGI).

    from fixwire.integrations.asgi import FixwireMiddleware
    app.add_middleware(FixwireMiddleware)        # FastAPI / Starlette
    app = FixwireMiddleware(app)                 # any ASGI app

A pure ASGI middleware (not BaseHTTPMiddleware): each request gets its own
isolation scope, unhandled exceptions are reported with the request and the
route, the body is never read, and lifespan shutdown flushes without
blocking the loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import fixwire
from fixwire.integrations._request import pii, processor, request_info, session

if TYPE_CHECKING:
    from fixwire.types import Request


class FixwireMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await self.app(scope, self._lifespan_receive(receive), send)
            return
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        # The scope as the request arrived: routing changes it as it goes
        # (a mount moves its prefix to root_path).
        initial = dict(scope)
        with fixwire.isolation_scope() as iso, session():
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or ()}
            fixwire.continue_trace(headers)
            iso.add_event_processor(processor(lambda: _request(initial), lambda: _route(initial, scope, self.app)))
            method = scope.get("method", "WEBSOCKET")
            attrs = {"http.request.method": method, "url.path": scope.get("path"), "url.scheme": scope.get("scheme")}
            with fixwire.start_span(
                "%s %s" % (method, scope.get("path", "")), op="http.server", origin="auto.http.asgi", attributes=attrs
            ) as span:
                started = False

                async def send_wrapper(message: dict[str, Any]) -> None:
                    nonlocal started
                    if message.get("type") == "http.response.start":
                        started = True
                        span.set_attribute("http.response.status_code", message.get("status"))
                        if int(message.get("status") or 0) >= 500:
                            span.set_status("error")
                    await send(message)

                try:
                    await self.app(scope, receive, send_wrapper)
                except Exception as e:
                    if not started and kind == "http":
                        span.set_attribute("http.response.status_code", 500)  # what the server answers
                    client = fixwire.get_client()
                    if client is not None:
                        client.capture_exception(e, mechanism={"type": "asgi", "handled": False})
                    raise
                finally:
                    route = _route(initial, scope, self.app)
                    if route:
                        span.update_name("%s %s" % (method, route))
                        span.set_attribute("http.route", route)

    @staticmethod
    def _lifespan_receive(receive: Any) -> Any:
        async def wrapped() -> dict[str, Any]:
            message: dict[str, Any] = await receive()
            if message.get("type") == "lifespan.shutdown":
                await fixwire.aflush()
            return message

        return wrapped


def _request(scope: dict[str, Any]) -> Request:
    headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers") or ()]
    host = next((v for k, v in headers if k.lower() == "host"), None)
    if host is None and scope.get("server"):
        h, p = scope["server"]
        host = "%s:%s" % (h, p) if p not in (80, 443, None) else str(h)
    scheme = scope.get("scheme", "http")
    url = "%s://%s%s%s" % (scheme, host or "", scope.get("root_path", ""), scope.get("path", ""))
    method = scope.get("method", "GET" if scope.get("type") == "http" else "WEBSOCKET")
    return request_info(method, url, (scope.get("query_string") or b"").decode("latin-1"), headers, pii())


def _route(initial: dict[str, Any], scope: dict[str, Any], app: Any) -> str | None:
    """The template of the route that served the request: matched against the
    app's routes as the request arrived, mounts included (Starlette, FastAPI);
    else the route the framework left in the scope; else the path."""
    routes = getattr(initial.get("app"), "routes", None) or getattr(app, "routes", None)
    path = _matched_path(routes, initial)
    if path:
        return path
    route = scope.get("route")
    return getattr(route, "path", None) or getattr(route, "path_format", None) or initial.get("path")


def _matched_path(routes: Any, scope: dict[str, Any]) -> str | None:
    """Matches as Starlette's router does: the first full match, else the first
    partial one (a path that matches with another method). A mount adds its
    prefix to the template of the route it matched inside."""
    if not routes:
        return None
    try:
        from starlette.routing import Host, Match, Mount
    except ImportError:
        return None
    partial: tuple[Any, dict[str, Any]] | None = None
    try:
        for route in routes:
            match, child = route.matches(scope)
            if match == Match.FULL:
                partial = (route, child)
                break
            if match == Match.PARTIAL and partial is None:
                partial = (route, child)
    except Exception:  # a route of another kind: no template
        return None
    if partial is None:
        return None
    route, child = partial
    if isinstance(route, (Mount, Host)):
        inner = _matched_path(route.routes, {**scope, **child})
        prefix = route.path if isinstance(route, Mount) else ""
        return prefix + inner if inner else (prefix or None)
    return getattr(route, "path", None)
