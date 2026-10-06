"""Turns event values into JSON-safe data within limits: strings capped in
UTF-8 bytes, containers capped in depth, breadth and number, cycles cut,
everything else through a bounded repr.

Strings are cut twice: first to what redaction reads (the part kept and the
next 16 kB, so a secret the cut goes through is found whole), then, once
redacted, to max_value_length (clip_strings).
"""

from __future__ import annotations

import datetime
import itertools
import math
import reprlib
import uuid
from collections.abc import Iterable
from typing import Any, cast

MAX_DEPTH = 10
MAX_BREADTH = 100
#: Containers walked per value; past it they are "[Object]" or "[Array]".
MAX_OBJECTS = 10_000
#: Bytes past a cut that redaction still reads.
REDACT_LOOKAHEAD = 16 << 10
#: A container inside itself (as the JavaScript SDK writes it).
CIRCULAR = "[Circular ~]"
#: A value that raised when read.
UNREADABLE = "[Unreadable]"

_PRIMITIVES = (bool, int, float, type(None))


def clip(s: str, limit: int) -> str:
    """At most ``limit`` bytes of UTF-8 (0: no limit), cut on a character
    boundary and ending in "..." (within the limit)."""
    if not limit or len(s) <= limit // 4:
        return s
    data = s[: limit + 1].encode("utf-8", "surrogatepass")
    if len(data) <= limit:
        return s
    cut = max(0, limit - 3)
    while cut and data[cut] & 0xC0 == 0x80:  # inside a character
        cut -= 1
    return data[:cut].decode("utf-8", "surrogatepass") + "..."[:limit]


def window(limit: int) -> int:
    """What redaction reads of a string cut to ``limit``: the part kept and
    the next 16 kB."""
    return limit + REDACT_LOOKAHEAD if limit else 0


def clip_strings(value: Any, limit: int) -> Any:
    """A serialized (JSON-like) value with every string, keys too, at most
    ``limit`` bytes."""
    if isinstance(value, str):
        return clip(value, limit)
    if isinstance(value, dict):
        items = cast("dict[object, Any]", value).items()
        return {clip(k, limit) if isinstance(k, str) else k: clip_strings(v, limit) for k, v in items}
    if isinstance(value, list):
        return [clip_strings(v, limit) for v in cast("list[object]", value)]
    return value


def number(value: float) -> float | str:
    """A float as JSON has it: NaN and the infinities as strings."""
    if math.isfinite(value):
        return value
    return "NaN" if value != value else "Infinity" if value > 0 else "-Infinity"


class BoundedRepr(reprlib.Repr):
    """reprlib's bounded reprs, but an object whose repr raises is
    "[Unreadable]"."""

    def repr_instance(self, x: Any, level: int) -> str:
        try:
            s = repr(x)
        except Exception:
            return UNREADABLE
        if len(s) > self.maxother:
            i = max(0, (self.maxother - 3) // 2)
            j = max(0, self.maxother - 3 - i)
            s = s[:i] + "..." + s[len(s) - j :]
        return s


class Serializer:
    def __init__(self, max_value_length: int = 1024) -> None:
        self.limit = max_value_length
        self._repr = BoundedRepr()
        self._repr.maxstring = self._repr.maxother = max(16, max_value_length)

    def __call__(self, value: Any, depth: int = 0) -> Any:
        return self._value(value, depth, set(), [MAX_OBJECTS])

    def _value(self, value: Any, depth: int, parents: set[int], budget: list[int]) -> Any:
        """``parents``: the ids of the containers ``value`` is in;
        ``budget``: the containers that may still be walked."""
        if isinstance(value, str):
            return clip(value, self.limit)
        if isinstance(value, _PRIMITIVES):
            return number(value) if isinstance(value, float) else value
        if isinstance(value, (dict, list, tuple, set, frozenset)):
            if depth >= MAX_DEPTH or budget[0] <= 0:
                return "[Object]" if isinstance(value, dict) else "[Array]"
            budget[0] -= 1
            key = id(cast("object", value))
            if key in parents:
                return CIRCULAR
            parents.add(key)
            try:
                if isinstance(value, dict):
                    out: dict[str, Any] = {}
                    items = cast("dict[object, object]", value).items()
                    for k, v in itertools.islice(items, MAX_BREADTH):
                        name = clip(k, self.limit) if isinstance(k, str) else self._safe_repr(k)
                        out[name] = self._value(v, depth + 1, parents, budget)
                    return out
                values = itertools.islice(cast("Iterable[object]", value), MAX_BREADTH)
                return [self._value(v, depth + 1, parents, budget) for v in values]
            except Exception:  # a subclass whose items() or iteration raises
                return UNREADABLE
            finally:
                parents.discard(key)
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, bytes):
            # Only what can survive the cap is decoded (a byte is a byte or more).
            head = value[: self.limit + 4] if self.limit else value
            return clip(head.decode("utf-8", "replace"), self.limit)
        return clip(self._safe_repr(value), self.limit)

    def _safe_repr(self, value: Any) -> str:
        try:
            return self._repr.repr(value)
        except Exception:
            return UNREADABLE
