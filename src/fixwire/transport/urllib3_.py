from __future__ import annotations

from typing import Any

import urllib3

from fixwire.transport import in_sdk_request


class Urllib3Sender:
    """One keep-alive connection; retries are the delivery's job."""

    def __init__(self, timeout: float) -> None:
        self._pool = urllib3.PoolManager(num_pools=2, maxsize=1, retries=False, timeout=urllib3.Timeout(total=timeout))

    def __call__(self, url: str, body: bytes, headers: dict[str, Any]) -> tuple[Any, ...]:
        token = in_sdk_request.set(True)
        try:
            res = self._pool.request("POST", url, body=body, headers=headers, preload_content=True)
        finally:
            in_sdk_request.reset(token)
        return res.status, {k.lower(): v for k, v in res.headers.items()}

    def close(self) -> None:
        self._pool.clear()
