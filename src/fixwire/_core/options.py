"""Client options: typed, with safe defaults and environment fallbacks."""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fixwire.types import BeforeBreadcrumb, BeforeSend, Integration, TracesSampler


def env(name: str) -> str | None:
    """The FIXWIRE_<name> environment variable, unless empty."""
    return os.environ.get("FIXWIRE_" + name) or None


@dataclass
class RateLimit:
    """Client-side budgets: a crash loop costs a few events and a count."""

    #: Events per issue (fingerprint) sent in a burst, then per minute.
    per_issue_burst: int = 10
    per_issue_per_minute: float = 1.0
    #: Events per minute across all issues.
    global_per_minute: float = 600.0
    enabled: bool = True


@dataclass
class Options:
    dsn: str | None = None
    release: str | None = None
    environment: str | None = None
    dist: str | None = None
    server_name: str | None = None
    #: Share of events sent, after the budgets.
    sample_rate: float = 1.0
    #: Share of traces recorded (0.0–1.0); None turns tracing off.
    traces_sample_rate: float | None = None
    #: A function deciding per trace: (context dict) → rate or bool.
    traces_sampler: TracesSampler | None = None
    #: Outgoing requests these match carry trace headers, the URL compared
    #: without its user info, query and fragment: a string with "://" is a
    #: URL prefix ("https://api.example.com/v2"), any other a host, with a
    #: port if it has one, matching it and its subdomains ("example.com":
    #: api.example.com, not badexample.com); a regex is searched for in the
    #: URL. Empty (the default): no headers leave the app.
    trace_propagation_targets: list[str | re.Pattern[str]] = field(default_factory=list[str | re.Pattern[str]])
    max_breadcrumbs: int = 100
    before_send: BeforeSend | None = None
    before_breadcrumb: BeforeBreadcrumb | None = None
    #: Local variables of in-app frames, bounded and redacted.
    include_local_variables: bool = True
    include_source_context: bool = True
    #: Longest string sent, in bytes of UTF-8: longer ones are cut on a
    #: character boundary and end in "..." (redaction reads 16 kB past the cut).
    max_value_length: int = 1024
    #: Stack frames kept per exception (the oldest are dropped first).
    max_stack_frames: int = 100
    in_app_include: list[str] = field(default_factory=list[str])
    in_app_exclude: list[str] = field(default_factory=list[str])
    #: Frames under this path are in-app (default: the working directory).
    project_root: str | None = None
    #: Mask secrets and personal data on the device (same rules as the server).
    redact: bool = True
    #: Replace the default sensitive key fragments.
    sensitive_keys: list[str] | None = None
    #: Send the user's IP address and email.
    send_default_pii: bool = False
    rate_limit: RateLimit = field(default_factory=RateLimit)
    #: "auto" (asyncio inside a running loop, else a thread), "thread" or "asyncio".
    transport: str = "auto"
    #: Seconds close() and process exit wait for pending events.
    shutdown_timeout: float = 2.0
    #: Seconds per HTTP request.
    http_timeout: float = 10.0
    #: Requests waiting to be sent, and as many waiting for a retry; past
    #: it new data is dropped.
    max_queue_size: int = 100
    #: Keep requests on disk until sent, across outages and restarts:
    #: True (a cache directory per DSN) or a file path. Off by default.
    offline: bool | str = False
    default_integrations: bool = True
    integrations: list[Integration] = field(default_factory=list["Integration"])
    debug: bool = False
    #: Record AI content (prompts, outputs, tool arguments and results) on
    #: gen_ai spans, bounded and redacted. Off: only models, tokens, timings,
    #: errors and tool-argument hashes are sent.
    record_ai_content: bool = False
    #: Release health: count each request as a session (exited, errored or
    #: crashed) so each release gets crash-free rates. Needs a release;
    #: sessions carry no content, and their user only as a hash made here.
    auto_session_tracking: bool = True

    def __post_init__(self) -> None:
        self.dsn = self.dsn if self.dsn is not None else env("DSN")
        self.release = self.release or env("RELEASE")
        self.environment = self.environment or env("ENVIRONMENT") or "production"
        self.server_name = self.server_name or _hostname()
        if self.project_root is None:
            self.project_root = os.getcwd()
        if not 0.0 <= self.sample_rate <= 1.0:
            raise ValueError("sample_rate must be between 0 and 1")
        if self.max_breadcrumbs < 0:
            raise ValueError("max_breadcrumbs must be 0 or more")

    @classmethod
    def from_kwargs(cls, dsn: str | None = None, **kwargs: Any) -> Options:
        known = {f.name for f in fields(cls)}
        unknown = set(kwargs) - known
        if unknown:
            raise TypeError("unknown option(s): %s" % ", ".join(sorted(unknown)))
        if isinstance(kwargs.get("rate_limit"), dict):
            kwargs["rate_limit"] = RateLimit(**kwargs["rate_limit"])
        return cls(dsn=dsn, **kwargs)

    @classmethod
    def off(cls) -> Options:
        """The options of an SDK that stays off: no DSN (FIXWIRE_DSN isn't
        read either) and no integrations."""
        return cls(dsn="", default_integrations=False)


def _hostname() -> str | None:
    try:
        return socket.gethostname()
    except Exception:
        return None
