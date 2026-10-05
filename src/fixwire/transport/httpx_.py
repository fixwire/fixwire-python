from __future__ import annotations

from typing import Any

import httpx

from fixwire.transport import in_sdk_request


class HttpxSender:
    def __init__(self, timeout: float) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout, limits=httpx.Limits(max_connections=4, max_keepalive_connections=4)
        )

    async def __call__(self, url: str, body: bytes, headers: dict[str, Any]) -> tuple[Any, ...]:
        token = in_sdk_request.set(True)
        try:
            res = await self._client.post(url, content=body, headers=headers)
        finally:
            in_sdk_request.reset(token)
        return res.status_code, {k.lower(): v for k, v in res.headers.items()}

    async def aclose(self) -> None:
        await self._client.aclose()
