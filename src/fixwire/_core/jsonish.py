"""Typed views of JSON-like values whose shape isn't trusted (events built
by users, payloads from other libraries): a wrong shape reads as empty."""

from __future__ import annotations

from typing import Any, cast


def as_dict(value: object) -> dict[str, Any] | None:
    """The value when it's a dict, else None."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def get_dict(value: object) -> dict[str, Any]:
    """The value when it's a dict, else an empty (unshared) one."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """The value when it's a list, else an empty one."""
    return cast("list[object]", value) if isinstance(value, list) else []


def dicts(value: object) -> list[dict[str, Any]]:
    """The dict items of a list (anything else: none)."""
    return [d for d in map(as_dict, as_list(value)) if d is not None]
