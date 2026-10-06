"""Turns event values into JSON-safe data within limits: strings capped at
max_value_length, containers capped in depth and breadth, cycles cut,
everything else through a bounded repr."""

from __future__ import annotations

import datetime
import itertools
import reprlib
import uuid
from collections.abc import Iterable
from typing import Any, cast

MAX_DEPTH = 10
MAX_BREADTH = 100
#: A container inside itself (as the JavaScript SDK writes it).
CIRCULAR = "[Circular ~]"

_PRIMITIVES = (bool, int, float, type(None))


def clip(s: str, limit: int) -> str:
    if limit and len(s) > limit:
        return s[: max(0, limit - 3)] + "..."
    return s


class Serializer:
    def __init__(self, max_value_length: int = 1024) -> None:
        self.limit = max_value_length
        self._repr = reprlib.Repr()
        self._repr.maxstring = self._repr.maxother = max(16, max_value_length)

    def __call__(self, value: Any, depth: int = 0) -> Any:
        return self._value(value, depth, set())

    def _value(self, value: Any, depth: int, parents: set[int]) -> Any:
        """``parents``: the ids of the containers ``value`` is in."""
        if isinstance(value, str):
            return clip(value, self.limit)
        if isinstance(value, _PRIMITIVES):
            if isinstance(value, float) and value != value:  # NaN is not JSON
                return None
            return value
        if depth >= MAX_DEPTH:
            return clip(self._safe_repr(value), self.limit)
        if isinstance(value, (dict, list, tuple, set, frozenset)):
            key = id(cast("object", value))
            if key in parents:
                return CIRCULAR
            parents.add(key)
            try:
                if isinstance(value, dict):
                    out: dict[str, Any] = {}
                    items = cast("dict[object, object]", value).items()
                    for k, v in itertools.islice(items, MAX_BREADTH):
                        out[k if isinstance(k, str) else self._safe_repr(k)] = self._value(v, depth + 1, parents)
                    return out
                values = itertools.islice(cast("Iterable[object]", value), MAX_BREADTH)
                return [self._value(v, depth + 1, parents) for v in values]
            finally:
                parents.discard(key)
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, bytes):
            # Only what can survive the cap is decoded (4 bytes a character at most).
            head = value[: self.limit * 4 + 4] if self.limit else value
            return clip(head.decode("utf-8", "replace"), self.limit)
        return clip(self._safe_repr(value), self.limit)

    def _safe_repr(self, value: Any) -> str:
        try:
            return self._repr.repr(value)
        except Exception:
            return "<broken repr>"
