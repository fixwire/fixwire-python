"""HTTP backends: a sync one (urllib3) for the thread driver, an async one
(httpx) for the asyncio driver. Each returns (status, lower-cased headers)."""

from contextvars import ContextVar

#: Set while the SDK sends, so HTTP integrations skip their own requests.
in_sdk_request: ContextVar[bool] = ContextVar("fixwire_in_sdk_request", default=False)

#: Bytes of an answer's body read at most (the ingest answers a little JSON).
MAX_RESPONSE_BYTES = 64 << 10
