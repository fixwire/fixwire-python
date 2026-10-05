"""Delivery as a sans-IO state machine: the queue policy, retries with
backoff and rate limits. Drivers (a thread, an asyncio task) feed it time
and HTTP outcomes and ask what to send next; it never sleeps, reads a clock
or opens a socket, which is what lets the sync and async clients share it.

Each queued item is one request: a /v1 path, a content type and a body.

Retries: network errors, 429 and 5xx, with exponential backoff and jitter
from 1 s to 5 min (at least Retry-After), up to 6 attempts. Other 4xx are
final: the server will not change its mind. Fixwire-Rate-Limits
("60:log;span, 3600:file"; no categories: all of them) pauses those kinds
of data while the rest keeps flowing; paused requests wait in the queue, and
are the first to go when it is full.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

BACKOFF_BASE = 1.0
BACKOFF_MAX = 300.0
MAX_ATTEMPTS = 6
#: Seconds a 429 without Retry-After waits.
DEFAULT_RETRY_AFTER = 60.0


@dataclass
class Outbound:
    """One request on its way."""

    #: Relative to the DSN's base URL, e.g. "/v1/logs".
    path: str
    content_type: str
    #: Compressed (gzip).
    body: bytes
    #: Its rate-limit category: error, log, span, session, check_in, feedback or file.
    category: str
    attempts: int = 0
    not_before: float = 0.0
    #: Its row in the offline spool, if one is on.
    spool_id: int | None = None


@dataclass
class Decision:
    sent: bool = False
    retry: bool = False
    dropped: bool = False
    reason: str = ""


def parse_rate_limits(header: str, now: float) -> dict[str, float]:
    """Fixwire-Rate-Limits ("60:log;span, 3600:file") to {category: until};
    "" (an empty category list) means every category."""
    out: dict[str, float] = {}
    for limit in header.split(","):
        seconds, sep, categories = limit.strip().partition(":")
        if not sep:
            continue
        try:
            until = now + float(seconds)
        except ValueError:
            continue
        for cat in [c.strip() for c in categories.split(";") if c.strip()] or [""]:
            out[cat] = max(out.get(cat, 0.0), until)
    return out


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


@dataclass
class Delivery:
    max_items: int = 100
    max_bytes: int = 32 << 20
    rng: Callable[[], float] = random.random
    queue: deque[Outbound] = field(default_factory=deque[Outbound])
    limits: dict[str, float] = field(default_factory=dict[str, float])
    queued_bytes: int = 0
    #: Requests dropped because the queue was full.
    overflowed: int = 0

    def offer(self, item: Outbound, now: float) -> None:
        """Queues a request. Past max_items or max_bytes one goes: the
        oldest paused by a rate limit, else the oldest."""
        self.queue.append(item)
        self.queued_bytes += len(item.body)
        while len(self.queue) > self.max_items or (self.queued_bytes > self.max_bytes and len(self.queue) > 1):
            idx = next((i for i, q in enumerate(self.queue) if self.paused_until(q.category) > now), 0)
            old = self.queue[idx]
            del self.queue[idx]
            self.queued_bytes -= len(old.body)
            self.overflowed += 1

    def paused_until(self, category: str) -> float:
        """Until when a rate limit holds this category back."""
        return max(self.limits.get("", 0.0), self.limits.get(category, 0.0))

    def ready_at(self, item: Outbound) -> float:
        return max(item.not_before, self.paused_until(item.category))

    def limited(self, category: str, now: float) -> bool:
        return self.paused_until(category) > now

    def next(self, now: float) -> Outbound | None:
        """The oldest request that may go now, or None (see wake_at)."""
        for i, item in enumerate(self.queue):
            if self.ready_at(item) <= now:
                del self.queue[i]
                self.queued_bytes -= len(item.body)
                return item
        return None

    def wake_at(self) -> float | None:
        """When a waiting request may go; None if none waits."""
        return min((self.ready_at(i) for i in self.queue), default=None)

    def on_response(self, item: Outbound, status: int, headers: dict[str, str], now: float) -> Decision:
        limits = headers.get("fixwire-rate-limits")
        if limits:
            for cat, until in parse_rate_limits(limits, now).items():
                self.limits[cat] = max(self.limits.get(cat, 0.0), until)
        if 200 <= status < 300:
            return Decision(sent=True)
        if status != 429 and status < 500:
            return Decision(dropped=True, reason="status %d" % status)
        wait = _retry_after(headers.get("retry-after"))
        if status == 429:
            wait = DEFAULT_RETRY_AFTER if wait is None else wait
            if not limits:
                # Rate limited without saying which data: everything waits.
                self.limits[""] = max(self.limits.get("", 0.0), now + wait)
        return self._retry(item, now, "status %d" % status, wait)

    def on_error(self, item: Outbound, now: float, err: str = "network error") -> Decision:
        return self._retry(item, now, err)

    def _retry(self, item: Outbound, now: float, reason: str, wait: float | None = None) -> Decision:
        item.attempts += 1
        if item.attempts >= MAX_ATTEMPTS:
            return Decision(dropped=True, reason=reason)
        delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (item.attempts - 1))) * (0.5 + self.rng() / 2)
        item.not_before = now + max(delay, wait or 0.0)
        self.queue.appendleft(item)
        self.queued_bytes += len(item.body)
        return Decision(retry=True, reason=reason)

    def empty(self) -> bool:
        return not self.queue
