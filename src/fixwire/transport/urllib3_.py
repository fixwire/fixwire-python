from __future__ import annotations

import contextlib
from typing import Any

import urllib3

from fixwire.transport import MAX_RESPONSE_BYTES, in_sdk_request


class Urllib3Sender:
    """One keep-alive connection; retries are the delivery's job. Redirects
    are not followed (the key stays with the ingest)."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._pool = self._new_pool()

    def _new_pool(self) -> urllib3.PoolManager:
        return urllib3.PoolManager(num_pools=2, maxsize=1, retries=False, timeout=urllib3.Timeout(total=self._timeout))

    def after_fork(self) -> None:
        """In a forked child: connections of its own. The parent's (a TLS
        session shared by two processes breaks) are left to the parent,
        untouched, as their locks may be held."""
        self._pool = self._new_pool()

    def __call__(self, url: str, body: bytes, headers: dict[str, Any]) -> tuple[Any, ...]:
        token = in_sdk_request.set(True)
        try:
            res = self._pool.request("POST", url, body=body, headers=headers, redirect=False, preload_content=False)
            # Only the status and headers matter: a short body is read so the
            # connection is kept, a longer (or broken) one closes it.
            whole = False
            try:
                with contextlib.suppress(Exception):
                    whole = len(res.read(MAX_RESPONSE_BYTES + 1, decode_content=False)) <= MAX_RESPONSE_BYTES
            finally:
                if not whole:
                    res.close()
                res.release_conn()
        finally:
            in_sdk_request.reset(token)
        return res.status, {k.lower(): v for k, v in res.headers.items()}

    def close(self) -> None:
        self._pool.clear()
