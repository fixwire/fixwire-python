"""httpx (sync and async): outgoing requests inside a span become child
spans, and carry trace headers to hosts in trace_propagation_targets.

    fixwire.init(dsn=..., traces_sample_rate=0.2, integrations=[HttpxIntegration()],
                 trace_propagation_targets=["api.internal.example"])

It wraps the public Client.send and AsyncClient.send, fails open (any error
of ours falls back to the plain call) and skips the SDK's own requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fixwire.integrations import install_once
from fixwire.integrations._outgoing import Outgoing, begin

if TYPE_CHECKING:
    from fixwire.client import Client


class HttpxIntegration:
    def setup(self, client: Client) -> None:
        if not install_once("httpx"):
            return
        import httpx

        sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send

        def send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            out = _begin(request)
            try:
                response = sync_send(self, request, *args, **kwargs)
            except Exception as e:
                if out is not None:
                    out.end(error=e)
                raise
            if out is not None:
                out.end(response.status_code)
            return response

        async def asend(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            out = _begin(request)
            try:
                response = await async_send(self, request, *args, **kwargs)
            except Exception as e:
                if out is not None:
                    out.end(error=e)
                raise
            if out is not None:
                out.end(response.status_code)
            return response

        setattr(httpx.Client, "send", send)  # noqa: B010
        setattr(httpx.AsyncClient, "send", asend)  # noqa: B010


def _begin(request: Any) -> Outgoing | None:
    return begin(str(request.method), str(request.url), request.url.host, request.headers, "auto.http.httpx")
