from __future__ import annotations

import contextlib
from typing import Any

import httpx

from fixwire.transport import MAX_RESPONSE_BYTES, in_sdk_request


class HttpxSender:
    """Redirects are not followed (the key stays with the ingest)."""

    def __init__(self, timeout: float) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout, limits=httpx.Limits(max_connections=4, max_keepalive_connections=4), follow_redirects=False
        )

    async def __call__(self, url: str, body: bytes, headers: dict[str, Any]) -> tuple[Any, ...]:
        token = in_sdk_request.set(True)
        try:
            async with self._client.stream("POST", url, content=body, headers=headers) as res:
                # Only the status and headers matter: a short body is read so
                # the connection is kept, a longer one closes it.
                read = 0
                with contextlib.suppress(httpx.HTTPError):
                    async for chunk in res.aiter_raw():
                        read += len(chunk)
                        if read > MAX_RESPONSE_BYTES:
                            break
        finally:
            in_sdk_request.reset(token)
        return res.status_code, {k.lower(): v for k, v in res.headers.items()}

    async def aclose(self) -> None:
        await self._client.aclose()
