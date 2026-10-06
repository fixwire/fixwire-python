"""Types for editors and type checkers: what events, hints, breadcrumbs and
options look like. Callbacks such as ``before_send`` receive these, so
their keys complete in the editor::

    def before_send(event: fixwire.types.Event, hint: fixwire.types.Hint) -> fixwire.types.Event | None:
        if event.get("logger") == "noisy":
            return None
        return event
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

if TYPE_CHECKING:
    from fixwire.client import Client

Level = Literal["debug", "info", "warning", "error", "fatal"]
"""Event and breadcrumb levels."""

ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]
"""What ``sys.exc_info()`` returns while an exception is handled."""


class User(TypedDict, total=False):
    id: str | int
    email: str
    username: str
    ip_address: str


class Breadcrumb(TypedDict, total=False):
    type: str
    """e.g. "http", "log", "navigation"."""
    category: str
    """What produced it, e.g. "cart" or a logger's name."""
    level: Level
    message: str
    data: dict[str, Any]
    timestamp: float
    """Unix seconds."""


class Frame(TypedDict, total=False):
    filename: str
    abs_path: str
    function: str
    module: str
    lineno: int
    colno: int
    in_app: bool
    """Your code (True) or a library's (False)."""
    pre_context: list[str]
    context_line: str
    post_context: list[str]
    vars: dict[str, str]
    """Local variables of in-app frames, as bounded reprs."""


class Stacktrace(TypedDict, total=False):
    frames: list[Frame]
    """Oldest first: the last frame raised."""


class Mechanism(TypedDict, total=False):
    type: str
    """How the error was captured: "generic", "asgi", "django", "celery", "logging", …"""
    handled: bool
    source: str
    exception_id: int
    parent_id: int
    is_exception_group: bool
    meta: dict[str, Any]


class ExceptionValue(TypedDict, total=False):
    type: str
    value: str
    module: str
    mechanism: Mechanism
    stacktrace: Stacktrace


class Exceptions(TypedDict):
    values: list[ExceptionValue]
    """Oldest first: the last one is the exception that was raised."""


class Request(TypedDict, total=False):
    method: str
    url: str
    """Without the query string."""
    query_string: str
    headers: dict[str, str]
    """Allowlisted headers only: never cookies or authorization."""


class Event(TypedDict, total=False):
    event_id: str
    timestamp: float
    platform: str
    level: Level
    logger: str
    message: str
    logentry: dict[str, Any]
    exception: Exceptions
    threads: dict[str, Any]
    transaction: str
    """The route, task or segment the event happened in."""
    tags: dict[str, str]
    extra: dict[str, Any]
    contexts: dict[str, dict[str, Any]]
    user: User
    breadcrumbs: dict[str, list[Breadcrumb]]
    fingerprint: list[str]
    """Overrides grouping: events with the same fingerprint are one issue."""
    release: str
    environment: str
    dist: str
    server_name: str
    request: Request
    sdk: dict[str, Any]


class Hint(TypedDict, total=False):
    exc_info: ExcInfo
    """The captured exception, when there is one."""
    log_record: logging.LogRecord
    """The log record, for events from the logging integration."""


class SamplingContext(TypedDict):
    name: str
    """The segment's name, e.g. "GET /items/{id}"."""
    attributes: dict[str, Any]
    parent_sampled: bool | None
    """The caller's decision, when the trace was continued."""


# Each callback may be typed with these TypedDicts (keys complete in the
# editor) or with plain dicts: both are accepted.

EventProcessor = (
    Callable[[Event, Hint], Event | None] | Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]
)
"""Changes an event, or drops it by returning None."""

BeforeSend = EventProcessor
"""The last word on an event: return it (changed or not) or None to drop it."""

BeforeBreadcrumb = (
    Callable[[Breadcrumb, dict[str, Any]], Breadcrumb | None]
    | Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]
)
"""Changes a breadcrumb, or drops it by returning None."""

TracesSampler = Callable[[SamplingContext], float | bool | None] | Callable[[dict[str, Any]], float | bool | None]
"""Decides per trace: a rate (0.0–1.0), True/False, or None to use traces_sample_rate."""


CheckInStatus = Literal["in_progress", "ok", "error"]
"""A job's check-in: started, or finished well or badly."""


class MonitorSchedule(TypedDict, total=False):
    type: Literal["crontab", "interval"]
    value: str | int
    """A crontab ("0 3 * * *") or a number of units."""
    unit: Literal["minute", "hour", "day", "week", "month", "year"]
    """An interval's unit."""


class MonitorConfig(TypedDict, total=False):
    """Creates or updates a monitor from a check-in."""

    schedule: MonitorSchedule
    checkin_margin: int
    """Minutes a check-in may be late."""
    max_runtime: int
    """Minutes a run may take."""
    timezone: str
    """The schedule's time zone, e.g. "Europe/Berlin"."""


class Integration(Protocol):
    """Something that hooks the SDK into a library: ``setup`` runs once per
    ``init()``, and should install hooks only once per process."""

    def setup(self, client: Client) -> None: ...


class RateLimitOptions(TypedDict, total=False):
    per_issue_burst: int
    """Events per issue sent in a burst (default 10)."""
    per_issue_per_minute: float
    """Then this many per minute per issue (default 1)."""
    global_per_minute: float
    """Events per minute across issues (default 600)."""
    enabled: bool


class ClientOptions(TypedDict, total=False):
    """Keyword options of ``init()``, ``Client()`` and ``AsyncClient()``."""

    release: str | None
    """Your version, e.g. "api@1.4.0" (default: FIXWIRE_RELEASE)."""
    environment: str | None
    """e.g. "production" (default: FIXWIRE_ENVIRONMENT, then "production")."""
    dist: str | None
    server_name: str | None
    sample_rate: float
    """Share of error events sent, after the budgets (default 1.0)."""
    traces_sample_rate: float | None
    """Share of traces recorded, 0.0–1.0. None (the default) turns tracing off."""
    traces_sampler: TracesSampler | None
    trace_propagation_targets: list[str | re.Pattern[str]]
    """Where trace headers go, matched against the URL without user info, query and fragment: a URL prefix
    (a string with "://"), a host (and port) with its subdomains, or a compiled regex searched for. Empty: nowhere."""
    max_breadcrumbs: int
    before_send: BeforeSend | None
    before_breadcrumb: BeforeBreadcrumb | None
    include_local_variables: bool
    """Local variables of in-app frames, bounded and redacted (default True)."""
    include_source_context: bool
    max_value_length: int
    """Longest string sent, in bytes of UTF-8, "..." included (default 1024)."""
    max_stack_frames: int
    in_app_include: list[str]
    in_app_exclude: list[str]
    project_root: str | None
    redact: bool
    """Mask secrets and personal data on the device with the server's rules (default True)."""
    sensitive_keys: list[str] | None
    send_default_pii: bool
    """Send the user's IP address from proxy headers (default False)."""
    rate_limit: RateLimitOptions | Any
    transport: Literal["auto", "thread", "asyncio"]
    shutdown_timeout: float
    http_timeout: float
    max_queue_size: int
    """Requests waiting to be sent, and as many waiting for a retry (default 100); past it new data is dropped."""
    offline: bool | str
    """Keep requests on disk until sent: True (a cache directory) or a file path. Off by default."""
    default_integrations: bool
    integrations: list[Integration]
    debug: bool
    record_ai_content: bool
    """Record AI content (prompts, outputs, tool arguments) on gen_ai spans, bounded and redacted (default False)."""
    auto_session_tracking: bool
    """Release health: count each request as a session for crash-free rates (default True; needs release)."""
