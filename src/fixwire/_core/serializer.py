"""Turns event values into JSON-safe data within limits: strings capped at
max_value_length, containers capped in depth and breadth, everything else
through a bounded repr."""

from __future__ import annotations

import datetime
import reprlib
import uuid
from collections.abc import Iterable
from typing import Any, cast

MAX_DEPTH = 10
MAX_BREADTH = 100

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
        if isinstance(value, str):
            return clip(value, self.limit)
        if isinstance(value, _PRIMITIVES):
            if isinstance(value, float) and value != value:  # NaN is not JSON
                return None
            return value
        if depth >= MAX_DEPTH:
            return clip(self._safe_repr(value), self.limit)
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for i, (k, v) in enumerate(cast("dict[object, object]", value).items()):
                if i >= MAX_BREADTH:
                    break
                out[k if isinstance(k, str) else self._safe_repr(k)] = self(v, depth + 1)
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            items = cast("Iterable[object]", value)
            return [self(v, depth + 1) for i, v in enumerate(items) if i < MAX_BREADTH]
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, bytes):
            return clip(value.decode("utf-8", "replace"), self.limit)
        return clip(self._safe_repr(value), self.limit)

    def _safe_repr(self, value: Any) -> str:
        try:
            return self._repr.repr(value)
        except Exception:
            return "<broken repr>"
