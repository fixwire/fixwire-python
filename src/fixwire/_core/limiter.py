"""Client budgets: a fingerprint per event, a token bucket per fingerprint
and one across them. Suppressed occurrences are counted and ride on the
next event of that fingerprint (contexts.fixwire.suppressed), so issue
counts stay right while a crash loop costs a few events.

The fingerprint is cheap and only drives budgets; the server's grouping is
authoritative. It hashes the exception types and the top in-app frames,
normalized like the server's grouping v1, or the message with its variable
parts removed.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Any

from fixwire._core.jsonish import as_dict, as_list
from fixwire._core.jsonish import dicts as _dicts
from fixwire._core.jsonish import get_dict as _dict

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_MASK = 0xFFFFFFFFFFFFFFFF

# Emails are matched from the start of a word only, without "@" in either
# part: "\S+@\S+" backtracks cubically on text like "@@@…".
_NUMBERS = re.compile(
    r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b|"
    r"\b[0-9a-fA-F]{16,}\b|\d+(?:\.\d+)?|(?<![^\s@])[^\s@]+@[^\s@]+\.\w+"
)
_LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")
_TOP_FRAMES = 5
_LRU = 1024
#: Characters of a message the fingerprint reads.
_MAX_MESSAGE = 1024


def fnv1a(text: str) -> str:
    h = _FNV_OFFSET
    for b in text.encode("utf-8", "replace"):
        h = ((h ^ b) * _FNV_PRIME) & _MASK
    return "%016x" % h


def template(message: str) -> str:
    """A message (its start) with numbers, hex, UUIDs and emails replaced."""
    return _NUMBERS.sub("<*>", message[:_MAX_MESSAGE])


def fingerprint(event: dict[str, Any]) -> str:
    parts: list[str] = []
    values = _dicts(_dict(event.get("exception")).get("values"))
    if values:
        for v in values:
            parts.append(str(v.get("type") or ""))
        frames = _dicts(_dict(values[-1].get("stacktrace")).get("frames"))
        app = [f for f in frames if f.get("in_app")] or frames
        for f in app[-_TOP_FRAMES:]:
            where = f.get("module") or _LINE_SUFFIX.sub("", str(f.get("filename") or ""))
            parts.append("%s|%s" % (where, f.get("function") or ""))
        if not frames:
            parts.append(template(str(values[-1].get("value") or "")))
    else:
        msg: Any = event.get("message")
        m = as_dict(msg)
        if m is not None:
            msg = m.get("message") or m.get("formatted")
        parts.append(template(str(msg or _dict(event.get("logentry")).get("message", ""))))
    if event.get("fingerprint"):
        parts.append("\x1f".join(str(x) for x in as_list(event["fingerprint"])))
    return fnv1a("\x1e".join(parts))


class _Bucket:
    __slots__ = ("tokens", "updated", "suppressed", "first", "last")

    def __init__(self, tokens: float, now: float) -> None:
        self.tokens, self.updated = tokens, now
        self.suppressed = 0
        self.first = self.last = 0.0

    def take(self, burst: float, per_minute: float, now: float) -> bool:
        self.tokens = min(burst, self.tokens + (now - self.updated) * per_minute / 60.0)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class Limiter:
    """Thread-safe. Times are seconds (wall clock, for the suppressed range)."""

    def __init__(
        self, per_issue_burst: int, per_issue_per_minute: float, global_per_minute: float, enabled: bool = True
    ) -> None:
        self.burst, self.rate = float(per_issue_burst), per_issue_per_minute
        self.global_rate = global_per_minute
        self.enabled = enabled
        self._issues: OrderedDict[str, _Bucket] = OrderedDict()
        self._global = _Bucket(global_per_minute, 0.0)
        self._lock = threading.Lock()

    def after_fork(self) -> None:
        self._lock = threading.Lock()

    def allow(self, fp: str, now: float) -> tuple[bool, dict[str, Any] | None]:
        """Whether to send an event of this fingerprint, and, when sending,
        the occurrences suppressed since the last one sent:
        {"count", "first", "last"} or None."""
        if not self.enabled:
            return True, None
        with self._lock:
            b = self._issues.get(fp)
            if b is None:
                b = _Bucket(self.burst, now)
                self._issues[fp] = b
                if len(self._issues) > _LRU:
                    self._issues.popitem(last=False)
            else:
                self._issues.move_to_end(fp)
            if self._global.updated == 0.0:
                self._global.updated = now
            if b.take(self.burst, self.rate, now) and self._global.take(self.global_rate, self.global_rate, now):
                if b.suppressed:
                    out = {"count": b.suppressed, "first": b.first, "last": b.last}
                    b.suppressed = 0
                    return True, out
                return True, None
            if not b.suppressed:
                b.first = now
            b.suppressed += 1
            b.last = now
            return False, None

    def pending(self) -> dict[str, dict[str, Any]]:
        """Suppressed counts not yet reported, by fingerprint (cleared)."""
        with self._lock:
            out = {
                fp: {"count": b.suppressed, "first": b.first, "last": b.last}
                for fp, b in self._issues.items()
                if b.suppressed
            }
            for fp in out:
                self._issues[fp].suppressed = 0
            return out


def suppressed_context(info: dict[str, Any] | None) -> dict[str, Any] | None:
    return {"suppressed": info} if info else None
