"""Scopes, in three layers merged in this order into every event:

* global: the process (release, environment, init's initial scope);
* isolation: a request, task or job, forked by integrations; top-level
  set_tag/set_user/add_breadcrumb write here, and breadcrumbs live here;
* current: a new_scope() block.

Isolation and current scopes are held in ContextVars, so threads and
asyncio tasks each see their own.
"""

from __future__ import annotations

import contextlib
import copy
from collections import deque
from collections.abc import Generator
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

from fixwire._core.event_builder import safe_str
from fixwire._core.jsonish import dicts, get_dict

if TYPE_CHECKING:
    from fixwire._core.sessions import RequestSession
    from fixwire._core.tracing import PropagationContext
    from fixwire.client import Client
    from fixwire.types import Breadcrumb, EventProcessor, Level, User

LEVELS = ("debug", "info", "warning", "error", "fatal")


class Scope:
    __slots__ = (
        "tags",
        "extras",
        "contexts",
        "user",
        "level",
        "fingerprint",
        "breadcrumbs",
        "processors",
        "client",
        "propagation",
        "request_session",
    )

    def __init__(self, max_breadcrumbs: int = 100) -> None:
        self.tags: dict[str, str] = {}
        self.extras: dict[str, Any] = {}
        self.contexts: dict[str, dict[str, Any]] = {}
        self.user: User | None = None
        self.level: Level | None = None
        self.fingerprint: list[str] | None = None
        self.breadcrumbs: deque[Breadcrumb] = deque(maxlen=max_breadcrumbs)
        self.processors: list[EventProcessor] = []
        #: A client bound to this scope (several clients in one process).
        self.client: Client | None = None
        #: The trace of this request or job (isolation scopes).
        self.propagation: PropagationContext | None = None
        #: The session of the request this isolation scope serves (not forked).
        self.request_session: RequestSession | None = None

    def fork(self) -> Scope:
        s = Scope.__new__(Scope)
        s.tags, s.extras = dict(self.tags), dict(self.extras)
        s.contexts = {k: dict(v) for k, v in self.contexts.items()}
        s.user = self.user.copy() if self.user else None
        s.level, s.fingerprint = self.level, list(self.fingerprint) if self.fingerprint else None
        s.breadcrumbs = deque(self.breadcrumbs, maxlen=self.breadcrumbs.maxlen)
        s.processors, s.client = list(self.processors), self.client
        s.propagation = self.propagation
        s.request_session = None
        return s

    def set_tag(self, key: str, value: object) -> None:
        """A searchable key/value on events (values become strings)."""
        self.tags[key] = value if isinstance(value, str) else safe_str(value)

    def remove_tag(self, key: str) -> None:
        self.tags.pop(key, None)

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value

    def set_context(self, key: str, value: dict[str, Any] | None) -> None:
        """Structured data under ``contexts[key]``; None removes it."""
        if value is None:
            self.contexts.pop(key, None)
        else:
            self.contexts[key] = value

    def set_user(self, user: User | None) -> None:
        self.user = user.copy() if user else None

    def set_level(self, level: Level | None) -> None:
        """Overrides the level of events captured in this scope."""
        if level is not None and level not in LEVELS:
            raise ValueError("level must be one of %s" % ", ".join(LEVELS))
        self.level = level

    def set_fingerprint(self, fingerprint: list[str] | None) -> None:
        """Groups events of this scope by these values instead of the stack."""
        self.fingerprint = list(fingerprint) if fingerprint else None

    def add_breadcrumb(self, crumb: Breadcrumb) -> None:
        self.breadcrumbs.append(crumb)

    def clear_breadcrumbs(self) -> None:
        self.breadcrumbs.clear()

    def add_event_processor(self, fn: EventProcessor) -> None:
        """Runs ``fn(event, hint)`` on events of this scope; it returns the
        event (changed or not), or None to drop it."""
        self.processors.append(fn)

    def resize(self, max_breadcrumbs: int) -> None:
        if self.breadcrumbs.maxlen != max_breadcrumbs:
            self.breadcrumbs = deque(self.breadcrumbs, maxlen=max_breadcrumbs)


_global_scope = Scope()
# Outside any isolation_scope() or new_scope(), all code shares these two
# defaults on purpose; entering one forks them.
_isolation: ContextVar[Scope] = ContextVar("fixwire_isolation_scope", default=Scope())  # noqa: B039
_current: ContextVar[Scope] = ContextVar("fixwire_current_scope", default=Scope())  # noqa: B039


def get_global_scope() -> Scope:
    return _global_scope


def get_isolation_scope() -> Scope:
    return _isolation.get()


def get_current_scope() -> Scope:
    return _current.get()


@contextlib.contextmanager
def new_scope() -> Generator[Scope, None, None]:
    """A forked current scope for the block."""
    token = _current.set(_current.get().fork())
    try:
        yield _current.get()
    finally:
        _current.reset(token)


@contextlib.contextmanager
def isolation_scope() -> Generator[Scope, None, None]:
    """Forked isolation and current scopes, for a request or job."""
    itoken = _isolation.set(_isolation.get().fork())
    ctoken = _current.set(_current.get().fork())
    try:
        yield _isolation.get()
    finally:
        _current.reset(ctoken)
        _isolation.reset(itoken)


def propagation_context() -> PropagationContext:
    """The current isolation scope's trace (created on first use)."""
    from fixwire._core.tracing import PropagationContext

    iso = _isolation.get()
    if iso.propagation is None:
        iso.propagation = PropagationContext()
    return iso.propagation


def merged_breadcrumbs(limit: int) -> list[Breadcrumb]:
    crumbs = list(_global_scope.breadcrumbs) + list(_isolation.get().breadcrumbs) + list(_current.get().breadcrumbs)
    crumbs.sort(key=lambda c: c.get("timestamp", 0))
    return crumbs[-limit:] if limit else []


def apply(event: dict[str, Any], max_breadcrumbs: int) -> list[EventProcessor]:
    """Merges the three layers into an event (event fields win) and returns
    the event processors to run."""
    processors: list[EventProcessor] = []
    tags: dict[str, str] = {}
    extras: dict[str, Any] = {}
    contexts: dict[str, dict[str, Any]] = {}
    user: dict[str, Any] = {}
    level: Level | None = None
    fingerprint: list[str] | None = None
    for s in (_global_scope, _isolation.get(), _current.get()):
        tags.update(s.tags)
        extras.update(s.extras)
        for k, v in s.contexts.items():
            contexts[k] = copy.copy(v)
        if s.user:
            user.update(s.user)
        level = s.level or level
        fingerprint = s.fingerprint or fingerprint
        processors.extend(s.processors)
    if tags:
        event["tags"] = {**tags, **(event.get("tags") or {})}
    if extras:
        event["extra"] = {**extras, **(event.get("extra") or {})}
    if contexts:
        event["contexts"] = {**contexts, **(event.get("contexts") or {})}
    if user:
        event["user"] = {**user, **(event.get("user") or {})}
    if level:
        # A level set on a scope wins over the event's default.
        event["level"] = level
    if fingerprint and "fingerprint" not in event:
        event["fingerprint"] = fingerprint
    crumbs = merged_breadcrumbs(max_breadcrumbs)
    if crumbs:
        own = cast("list[Breadcrumb]", dicts(get_dict(event.get("breadcrumbs")).get("values")))
        event["breadcrumbs"] = {"values": crumbs + own}
    return processors
