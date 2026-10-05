"""The two clients. Both capture synchronously and cheaply from any thread
or task; they differ in how they deliver:

* Client: a background thread (urllib3).
* AsyncClient: a task on the owning event loop (httpx). If the loop goes
  away, what is left is handed to a thread so nothing is lost at shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
import uuid
import warnings
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

from fixwire._core.options import Options
from fixwire._core.pipeline import Core
from fixwire._core.scope import get_isolation_scope
from fixwire._core.sessions import RequestSession, identity

if TYPE_CHECKING:
    from typing_extensions import Self, Unpack

    from fixwire._core.tracing import Span
    from fixwire.drivers.asyncio_ import AsyncioDriver
    from fixwire.drivers.thread import ThreadDriver
    from fixwire.types import CheckInStatus, ClientOptions, Event, ExcInfo, Hint, Level, Mechanism, MonitorConfig

logger = logging.getLogger("fixwire")


class Client:
    """Captures events and delivers them from one background thread.

    Use it directly (scripts, workers, tests) or through ``fixwire.init()``::

        with fixwire.Client("https://<key>@<host>", release="job@1.0") as client:
            client.capture_message("started")
    """

    def __init__(self, dsn: str | None = None, **options: Unpack[ClientOptions]) -> None:
        self.options: Options = Options.from_kwargs(dsn, **options)
        self.core: Core = Core(self.options)
        self._thread_driver: ThreadDriver | None = None
        self._lock = threading.Lock()
        self._closed = False
        if self.core.spool is not None and type(self) is Client and self.core.spool.count():
            self._thread().start()

    @property
    def enabled(self) -> bool:
        """False without a DSN: captures are no-ops."""
        return self.core.dsn is not None and not self._closed

    # Capturing.

    def capture_event(self, event: Event | dict[str, Any], hint: Hint | None = None) -> str | None:
        """Sends an event you built (a dict shaped like ``fixwire.types.Event``).
        Returns its id, or None when it was dropped."""
        if not self.enabled:
            return None
        self.core.mark_session(cast("dict[str, Any]", event))
        try:
            prepared = self.core.prepare(cast("dict[str, Any]", event), hint)
        except Exception:
            logger.exception("fixwire: could not prepare an event")
            return None
        if prepared is None:
            return None
        self._submit(prepared)
        event_id: str = prepared["event_id"]
        return event_id

    def capture_exception(
        self, error: BaseException | ExcInfo | None = None, mechanism: Mechanism | None = None
    ) -> str | None:
        """Reports an exception (default: the one being handled). Returns
        the event id, or None when it was dropped."""
        if not self.enabled:
            return None
        try:
            event, hint = self.core.event_from_exception(error, mechanism)
        except ValueError as e:
            warnings.warn(str(e), stacklevel=2)
            return None
        exc = hint.get("exc_info", (None, None, None))[1]
        # The same exception object reported twice (explicitly, then by an
        # integration) is sent once.
        if getattr(exc, "__fixwire_captured__", False):
            return None
        event_id = self.capture_event(event, hint)
        if event_id is not None:
            try:
                setattr(exc, "__fixwire_captured__", True)  # noqa: B010
            except Exception:
                pass
        return event_id

    def capture_segment(self, segment: Span) -> None:
        """Queues a finished, sampled segment and its spans."""
        if self.enabled:
            self._submit(self.core.segment_payload(segment))

    def capture_message(self, message: str, level: Level = "info", stacktrace: bool = False) -> str | None:
        """Reports a message; ``stacktrace=True`` adds where it was called."""
        if not self.enabled:
            return None
        return self.capture_event(self.core.event_from_message(message, level, stacktrace))

    def capture_feedback(
        self,
        message: str | None = None,
        *,
        score: float | None = None,
        trace_id: str | None = None,
        event_id: str | None = None,
        url: str | None = None,
        source: str = "api",
    ) -> str | None:
        """Sends feedback: how someone rated an AI answer (``score`` from -1
        to 1 with the ``trace_id`` that produced it; default: the current
        trace) and/or what they said about a crash (``event_id``). A negative
        rating opens a user_feedback issue for the agent. Feedback isn't
        sampled or rate limited. Returns its id, or None when it holds
        neither a message nor a rating::

            client.capture_feedback("Refunded the wrong order", score=-1, trace_id=run_trace_id)
        """
        if not self.enabled:
            return None
        text = (message or "").strip()
        rating = 0.0
        if score is not None and math.isfinite(score):
            rating = max(-1.0, min(1.0, float(score)))
        if not text and rating == 0:
            return None
        item = self.core.feedback_item(text, rating, trace_id, event_id, url, source)
        self._submit(item)
        feedback_id: str = item["body"]["feedback_id"]
        return feedback_id

    def capture_check_in(
        self,
        monitor: str,
        status: CheckInStatus = "ok",
        *,
        check_in_id: str | None = None,
        duration: float | None = None,
        monitor_config: MonitorConfig | None = None,
    ) -> str | None:
        """Reports a run of a scheduled job to its monitor (``monitor`` is
        the monitor's slug): ``"in_progress"`` when it starts, then ``"ok"``
        or ``"error"`` with the same ``check_in_id`` (the first call returns
        it) and the run's ``duration`` in seconds. A single ``"ok"`` or
        ``"error"`` works too. ``monitor_config`` creates or updates the
        monitor. Check-ins aren't sampled or rate limited::

            run = client.capture_check_in("nightly-report", "in_progress",
                                          monitor_config={"schedule": {"type": "crontab", "value": "0 3 * * *"}})
            ...
            client.capture_check_in("nightly-report", "ok", check_in_id=run, duration=42.5)

        Returns the check-in's id, or None when nothing was sent."""
        if not self.enabled:
            return None
        if not monitor or len(monitor) > 128 or "/" in monitor:
            warnings.warn("a monitor's slug has 1 to 128 characters and no /", stacklevel=2)
            return None
        if status not in ("in_progress", "ok", "error"):
            warnings.warn("a check-in's status is in_progress, ok or error", stacklevel=2)
            return None
        check_in_id = (check_in_id or uuid.uuid4().hex).replace("-", "").lower()
        if duration is not None and not math.isfinite(duration):
            duration = None
        config = cast("dict[str, Any] | None", dict(monitor_config) if monitor_config else None)
        self._submit(self.core.check_in_item(monitor, status, check_in_id, duration, config))
        return check_in_id

    # Sessions (release health).

    def start_request_session(self) -> Callable[[], None]:
        """Starts the session of the request the current isolation scope
        serves; call the returned function once it ends. Integrations do
        this for you. A no-op without a release, or with
        ``auto_session_tracking=False``."""
        if not self.enabled or not self.core.sessions_on():
            return lambda: None
        iso = get_isolation_scope()
        session = RequestSession()
        iso.request_session = session
        done = False

        def end() -> None:
            nonlocal done
            if done:
                return
            done = True
            if self.core.sessions.record(session.status, identity(iso.user), time.time()):
                self._submit(self.core.sessions_item())

        return end

    def _send_sessions(self) -> None:
        """Queues the counted request sessions (before a flush or close)."""
        if self.enabled and len(self.core.sessions):
            self._submit(self.core.sessions_item())

    # Delivery.

    def _thread(self) -> ThreadDriver:
        with self._lock:
            if self._thread_driver is None:
                from fixwire.drivers.thread import ThreadDriver
                from fixwire.transport.urllib3_ import Urllib3Sender

                self._thread_driver = ThreadDriver(self.core, Urllib3Sender(self.options.http_timeout))
            return self._thread_driver

    def _submit(self, event: dict[str, Any]) -> None:
        self._thread().submit(event)

    def flush(self, timeout: float | None = None) -> bool:
        """Waits up to timeout seconds for queued events to be sent."""
        self._send_sessions()
        if self._thread_driver is None:
            return True
        return self._thread_driver.flush(self.options.shutdown_timeout if timeout is None else timeout)

    def close(self, timeout: float | None = None) -> None:
        """Sends what is queued (up to ``timeout``, default shutdown_timeout)
        and stops. Captures after close are no-ops."""
        if self._closed:
            return
        self._send_sessions()
        if self._thread_driver is not None:
            self._thread_driver.close(self.options.shutdown_timeout if timeout is None else timeout)
        self._closed = True

    async def aflush(self, timeout: float | None = None) -> bool:
        """Flushes without blocking the event loop."""
        return await asyncio.to_thread(self.flush, timeout)

    async def aclose(self, timeout: float | None = None) -> None:
        """``close()`` without blocking the event loop."""
        await asyncio.to_thread(self.close, timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()


class AsyncClient(Client):
    """Captures from anywhere and delivers from the event loop it was
    created on (or ``loop``), never blocking it::

        async with fixwire.AsyncClient("https://<key>@<host>") as client:
            client.capture_exception(error)
            await client.aflush()
    """

    def __init__(
        self, dsn: str | None = None, *, loop: asyncio.AbstractEventLoop | None = None, **options: Unpack[ClientOptions]
    ) -> None:
        super().__init__(dsn, **options)
        try:
            self.loop: asyncio.AbstractEventLoop = loop or asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("AsyncClient needs a running event loop (or pass loop=); use Client otherwise") from None
        self._async_driver: AsyncioDriver | None = None

    def _driver(self) -> AsyncioDriver:
        if self._async_driver is None:
            from fixwire.drivers.asyncio_ import AsyncioDriver
            from fixwire.transport.httpx_ import HttpxSender

            self._async_driver = AsyncioDriver(self.core, HttpxSender(self.options.http_timeout), self.loop)
        return self._async_driver

    def _submit(self, event: dict[str, Any]) -> None:
        driver = self._driver()
        if not driver.submit(event):
            # The loop is gone: deliver from a thread, with what it left.
            thread = self._thread()
            for item in driver.take_leftovers():
                thread.delivery.offer(item, 0.0)
            thread.submit(event)

    def _on_loop_thread(self) -> bool:
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    async def aflush(self, timeout: float | None = None) -> bool:
        timeout = self.options.shutdown_timeout if timeout is None else timeout
        self._send_sessions()
        ok = True
        if self._async_driver is not None and self._async_driver.usable:
            if self._on_loop_thread():
                ok = await self._async_driver.aflush(timeout)
            else:
                fut = asyncio.run_coroutine_threadsafe(self._async_driver.aflush(timeout), self.loop)
                ok = await asyncio.wrap_future(fut)
        if self._thread_driver is not None:
            ok = await asyncio.to_thread(self._thread_driver.flush, timeout) and ok
        return ok

    def flush(self, timeout: float | None = None) -> bool:
        """From another thread, waits for the loop to deliver. On the loop's
        own thread it can't wait without blocking it: use ``await aflush()``."""
        timeout = self.options.shutdown_timeout if timeout is None else timeout
        if self._on_loop_thread():
            warnings.warn("AsyncClient.flush() called on its event loop; use `await client.aflush()`", stacklevel=2)
            return False
        self._send_sessions()
        ok = True
        if self._async_driver is not None and self._async_driver.usable and self.loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._async_driver.aflush(timeout), self.loop)
            try:
                ok = fut.result(timeout + 1)
            except Exception:
                ok = False
        elif self._async_driver is not None:
            self._hand_over()
        if self._thread_driver is not None:
            ok = self._thread_driver.flush(timeout) and ok
        return ok

    def _hand_over(self) -> None:
        """Moves what the stopped loop left to the thread driver."""
        leftovers = self._async_driver.take_leftovers() if self._async_driver is not None else []
        events = self.core.queue.drain()
        if leftovers or events:
            thread = self._thread()
            for item in leftovers:
                thread.delivery.offer(item, 0.0)
            for e in events:
                thread.submit(e)

    async def aclose(self, timeout: float | None = None) -> None:
        if self._closed:
            return
        timeout = self.options.shutdown_timeout if timeout is None else timeout
        self._send_sessions()
        if self._async_driver is not None and self._async_driver.usable:
            if self._on_loop_thread():
                await self._async_driver.aclose(timeout)
            else:
                await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(self._async_driver.aclose(timeout), self.loop)
                )
        if self._thread_driver is not None:
            await asyncio.to_thread(self._thread_driver.close, timeout)
        self._closed = True

    def close(self, timeout: float | None = None) -> None:
        if self._closed:
            return
        if self._on_loop_thread():
            warnings.warn("AsyncClient.close() called on its event loop; use `await client.aclose()`", stacklevel=2)
            return
        self._send_sessions()
        if self._async_driver is not None:
            if self._async_driver.usable and self.loop.is_running():
                timeout_ = self.options.shutdown_timeout if timeout is None else timeout
                fut = asyncio.run_coroutine_threadsafe(self._async_driver.aclose(timeout_), self.loop)
                try:
                    fut.result(timeout_ + 1)
                except Exception:
                    pass
            else:
                self._hand_over()
        super().close(timeout)
