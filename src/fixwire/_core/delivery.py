"""Delivery as a sans-IO state machine: the queue policy, retries with
backoff and rate limits. Drivers (a thread, an asyncio task) feed it time
and HTTP outcomes and ask what to send next; it never sleeps, opens a
socket or reads a clock but the wall clock an HTTP-date Retry-After counts
from, which is what lets the sync and async clients share it.

Each queued item is one request: a /v1 path, a content type and a body.

Retries: no answer, 429 and 5xx, at most 3 times, waiting about 1 s, then
twice as long each time (at least Retry-After); a request whose next try
would be more than 5 minutes away is dropped. Other 4xx are final: the
server will not change its mind. A 429 without Fixwire-Rate-Limits pauses
all data for Retry-After, at least 60 s, and a 5xx with Retry-After pauses
it for that long. Fixwire-Rate-Limits ("60:log;span, 3600:file"; no
categories: all of them) pauses those kinds of data while the rest keeps
flowing; paused requests wait in the queue. At most max_items requests wait
to be sent and as many wait for a retry; past that, new ones are dropped.
"""

from __future__ import annotations

import email.utils
import math
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

BACKOFF_BASE = 1.0
#: The first try and 3 retries.
MAX_ATTEMPTS = 4
#: A request whose next try would be further away than this is dropped.
MAX_DELAY = 300.0
#: Seconds a 429 without Fixwire-Rate-Limits pauses all data at least.
DEFAULT_RETRY_AFTER = 60.0
#: The longest a Retry-After or a rate limit holds data back, in seconds.
MAX_WAIT = 24 * 3600.0
#: The rate-limit categories (fixwire-protocol §2); "" is all of them.
CATEGORIES = frozenset({"error", "log", "span", "session", "check_in", "feedback", "file"})


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
    "" (an empty category list) means every category. Categories Fixwire
    doesn't name are ignored."""
    out: dict[str, float] = {}
    for limit in header.split(","):
        seconds, sep, categories = limit.strip().partition(":")
        if not sep:
            continue
        try:
            until = now + _seconds(seconds)
        except ValueError:
            continue
        named = [c.strip() for c in categories.split(";") if c.strip()]
        for cat in [c for c in named if c in CATEGORIES] if named else [""]:
            out[cat] = max(out.get(cat, 0.0), until)
    return out


def _seconds(value: str) -> float:
    """A server's wait in seconds, from 0 to MAX_WAIT; ValueError unless
    a finite number."""
    seconds = float(value)
    if not math.isfinite(seconds):
        raise ValueError("not a finite number of seconds: %r" % value)
    return min(max(0.0, seconds), MAX_WAIT)


def _retry_after(value: str | None, wall: float) -> float | None:
    """Retry-After in seconds (from 0 to MAX_WAIT): a number of seconds or
    an HTTP date; None when missing or broken."""
    if not value:
        return None
    try:
        return _seconds(value)
    except ValueError:
        pass
    try:
        date = email.utils.parsedate_to_datetime(value)
        return min(max(0.0, date.timestamp() - wall), MAX_WAIT)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass
class Delivery:
    max_items: int = 100
    max_bytes: int = 32 << 20
    rng: Callable[[], float] = random.random
    #: The wall clock an HTTP-date Retry-After counts from.
    clock: Callable[[], float] = time.time
    queue: deque[Outbound] = field(default_factory=deque[Outbound])
    limits: dict[str, float] = field(default_factory=dict[str, float])
    queued_bytes: int = 0
    #: Requests in the queue waiting for a retry.
    retrying: int = 0
    #: Requests dropped because the queue was full.
    overflowed: int = 0

    def offer(self, item: Outbound, now: float) -> bool:
        """Queues a request; False when it is dropped: paused for longer
        than MAX_DELAY, or past max_items (or max_bytes) waiting."""
        if self.ready_at(item) - now > MAX_DELAY:
            return False
        return self._add(item, left=False)

    def _add(self, item: Outbound, left: bool) -> bool:
        """Queues a request unless max_items like it (new ones, or ones
        waiting for a retry) or max_bytes wait already."""
        waiting = self.retrying if item.attempts else len(self.queue) - self.retrying
        if waiting >= self.max_items or (self.queue and self.queued_bytes + len(item.body) > self.max_bytes):
            self.overflowed += 1
            return False
        if left:
            self.queue.appendleft(item)
        else:
            self.queue.append(item)
        self.queued_bytes += len(item.body)
        self.retrying += 1 if item.attempts else 0
        return True

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
                if item.attempts:
                    self.retrying -= 1
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
        wait = _retry_after(headers.get("retry-after"), self.clock())
        if status == 429 and not limits:
            # Rate limited without saying which data: everything waits.
            wait = max(wait or 0.0, DEFAULT_RETRY_AFTER)
            self.limits[""] = max(self.limits.get("", 0.0), now + wait)
        elif status >= 500 and wait is not None:
            # Unavailable for a while: everything waits.
            self.limits[""] = max(self.limits.get("", 0.0), now + wait)
        return self._retry(item, now, "status %d" % status, wait)

    def on_error(self, item: Outbound, now: float, err: str = "network error") -> Decision:
        return self._retry(item, now, err)

    def _retry(self, item: Outbound, now: float, reason: str, wait: float | None = None) -> Decision:
        item.attempts += 1
        if item.attempts >= MAX_ATTEMPTS:
            return Decision(dropped=True, reason=reason)
        delay = BACKOFF_BASE * (2 ** (item.attempts - 1)) * (0.5 + self.rng() / 2)
        item.not_before = now + max(delay, wait or 0.0)
        if self.ready_at(item) - now > MAX_DELAY:
            return Decision(dropped=True, reason=reason + "; the next try is too far away")
        if not self._add(item, left=True):
            return Decision(dropped=True, reason=reason + "; too many requests wait for a retry")
        return Decision(retry=True, reason=reason)

    def take(self) -> list[Outbound]:
        """Empties the queue (for another driver)."""
        out = list(self.queue)
        self.queue.clear()
        self.queued_bytes = self.retrying = 0
        return out

    def empty(self) -> bool:
        return not self.queue
