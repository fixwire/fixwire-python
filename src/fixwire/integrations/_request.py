"""Request context for events, private by default: headers come from an
allowlist (never cookies or authorization), the client's IP only with
send_default_pii, and query values go through redaction like the rest."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fixwire.types import Event, EventProcessor, Hint, Request

SAFE_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "content-length",
        "content-type",
        "host",
        "origin",
        "referer",
        "user-agent",
        "x-request-id",
        "x-correlation-id",
        "traceparent",
        "tracestate",
        "baggage",
    }
)
PII_HEADERS = frozenset({"x-forwarded-for", "x-real-ip", "forwarded"})


def request_info(
    method: str, url: str, query_string: str, headers: Iterable[tuple[str, str]], send_default_pii: bool
) -> Request:
    out: Request = {"method": method.upper(), "url": url}
    if query_string:
        out["query_string"] = query_string
    kept: dict[str, str] = {}
    for k, v in headers:
        name = k.lower()
        if name in SAFE_HEADERS or (send_default_pii and name in PII_HEADERS):
            kept[name] = v
    if kept:
        out["headers"] = kept
    return out


def processor(request: Callable[[], Request], transaction: Callable[[], str | None]) -> EventProcessor:
    """An event processor adding the request and the route, computed when
    an event is captured (the route is known by then)."""

    def process(event: Event, hint: Hint) -> Event:
        try:
            event.setdefault("request", request())
            if "transaction" not in event:
                name = transaction()
                if name:
                    event["transaction"] = name
        except Exception:
            pass
        return event

    return process


def pii() -> bool:
    from fixwire import get_client

    c = get_client()
    return c is not None and c.options.send_default_pii


@contextmanager
def session() -> Generator[None, None, None]:
    """The request's session (release health), inside its isolation scope:
    exited, errored or crashed by what was reported while it ran."""
    import fixwire

    client = fixwire.get_client()
    end = client.start_request_session() if client is not None else None
    try:
        yield
    finally:
        if end is not None:
            end()
