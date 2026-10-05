"""Flask.

    from fixwire.integrations.flask import init_app
    init_app(app)

Wraps ``app.wsgi_app`` with the WSGI middleware (a scope and a trace per
request, the URL rule as the transaction) and reports unhandled view errors
through Flask's ``got_request_exception`` signal: Flask turns them into 500
responses, so they never reach the middleware. HTTP errors (``abort(404)``)
are not reported. The error is sent once, even though Flask also logs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import fixwire
from fixwire.integrations.wsgi import ROUTE_KEY, FixwireMiddleware

if TYPE_CHECKING:
    from flask import Flask


def init_app(app: Flask) -> None:
    """Reports the app's errors and traces its requests (call once per app)."""
    from flask import got_request_exception, request_started

    setattr(app, "wsgi_app", FixwireMiddleware(app.wsgi_app))  # noqa: B010
    got_request_exception.connect(_report, app, weak=False)
    request_started.connect(_remember_route, app, weak=False)


def _remember_route(sender: Any, **_: Any) -> None:
    """Keeps the URL rule for the middleware, which reads it after Flask has
    torn the request down."""
    from flask import request

    if request.url_rule is not None:
        request.environ[ROUTE_KEY] = request.url_rule.rule


def _report(sender: Any, exception: BaseException, **_: Any) -> None:
    client = fixwire.get_client()
    if client is not None:
        client.capture_exception(exception, mechanism={"type": "flask", "handled": False})
