"""requests: outgoing requests inside a span become child spans, carry
trace headers to hosts in trace_propagation_targets, and leave an "http"
breadcrumb.

    fixwire.init(dsn=..., traces_sample_rate=0.2, integrations=[RequestsIntegration()],
                 trace_propagation_targets=["api.internal.example"])

It wraps the public Session.send (every requests call goes through it),
fails open and skips the SDK's own requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fixwire.integrations import install_once
from fixwire.integrations._outgoing import begin

if TYPE_CHECKING:
    from fixwire.client import Client


class RequestsIntegration:
    def setup(self, client: Client) -> None:
        if not install_once("requests"):
            return
        import requests

        original = requests.Session.send

        def send(self: Any, request: Any, **kwargs: Any) -> Any:
            from urllib.parse import urlsplit

            url = str(request.url)
            out = begin(str(request.method), url, urlsplit(url).hostname, request.headers, "auto.http.requests")
            try:
                response = original(self, request, **kwargs)
            except Exception as e:
                if out is not None:
                    out.end(error=e)
                raise
            if out is not None:
                out.end(response.status_code)
            return response

        setattr(requests.Session, "send", send)  # noqa: B010
