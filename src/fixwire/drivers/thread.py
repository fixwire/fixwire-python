"""The thread driver: one daemon thread for the whole client, one request
in flight. Captures only append to the core's queue and notify; the thread
encodes, sends and retries. Safe across fork(): the child starts clean and
leaves the parent's queue to the parent.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import weakref
from collections.abc import Callable, Iterable
from typing import Any

from fixwire._core.delivery import Delivery, Outbound
from fixwire._core.pipeline import Core

logger = logging.getLogger("fixwire")


class ThreadDriver:
    def __init__(self, core: Core, send: Callable[[str, bytes, dict[str, str]], tuple[int, dict[str, str]]]) -> None:
        self.core = core
        self.send = send
        self.delivery = Delivery()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._busy = False
        #: Wake the thread once without new events (to load the spool).
        self._kicked = False
        self._spool_loaded = False
        ref = weakref.ref(self)

        def after_fork() -> None:
            driver = ref()
            if driver is not None:
                driver._after_fork()

        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=after_fork)

    def start(self) -> None:
        """Starts delivering what an earlier run left in the spool."""
        with self._cond:
            self._ensure_thread()
            self._kicked = True
            self._cond.notify_all()

    def submit(self, event: dict[str, Any]) -> None:
        self.core.queue.put(event)
        with self._cond:
            self._ensure_thread()
            self._cond.notify_all()

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="fixwire-delivery", daemon=True)
            self._thread.start()

    def _pending(self) -> bool:
        return bool(len(self.core.queue)) or not self.delivery.empty() or self._busy or self._kicked

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._stopping and not len(self.core.queue) and not self._due() and not self._kicked:
                    wake = self.delivery.wake_at()
                    self._cond.wait(timeout=None if wake is None else max(0.0, wake - time.monotonic()))
                if self._stopping and not len(self.core.queue) and not self._due():
                    self._busy = False
                    self._cond.notify_all()
                    return
                self._busy = True
                self._kicked = False
            try:
                self._step()
            except Exception:  # never let the thread die
                logger.exception("fixwire: delivery failed")
            with self._cond:
                self._busy = False
                self._cond.notify_all()

    def _due(self) -> bool:
        wake = self.delivery.wake_at()
        return wake is not None and wake <= time.monotonic()

    def adopt(self, items: Iterable[Outbound], now: float = 0.0) -> None:
        """Queues encoded requests (what a stopped event loop left). The
        delivery is only touched under the lock: flush() reads it from other
        threads while this one sends."""
        with self._cond:
            for out in items:
                self.delivery.offer(out, now)

    def _step(self) -> None:
        now = time.monotonic()
        if not self._spool_loaded:
            self._spool_loaded = True
            self.adopt(self.core.spool_load(), now)
        for event in self.core.queue.drain():
            outs = self.core.encode(event)
            for out in outs:
                self.core.spool_put(out)
            self.adopt(outs, now)
        while True:
            with self._cond:
                item = self.delivery.next(time.monotonic())
            if item is None:
                return
            self._send(item)

    def _send(self, item: Outbound) -> None:
        try:
            status, headers = self.send(self.core.url(item), item.body, self.core.headers(item))
        except Exception as e:  # network errors are retried
            with self._cond:
                d = self.delivery.on_error(item, time.monotonic(), type(e).__name__)
        else:
            with self._cond:
                d = self.delivery.on_response(item, status, headers, time.monotonic())
        if not d.retry:
            self.core.spool_done(item)
        if d.dropped and self.core.options.debug:
            logger.warning("fixwire: dropped a request to %s (%s)", item.path, d.reason)

    def flush(self, timeout: float | None = None) -> bool:
        """Waits until everything queued is sent (or dropped); False on
        timeout, e.g. while a retry waits out its backoff or a rate limit."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            if self._pending():
                self._ensure_thread()
            self._cond.notify_all()
            while self._pending():
                if (
                    self.delivery.wake_at() is not None
                    and not len(self.core.queue)
                    and not self._busy
                    and not self._kicked
                    and not self._due()
                ):
                    return False  # only backoff waits remain
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cond.wait(timeout=left if left is not None else 0.5)
            return True

    def close(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        self.flush(timeout)
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # Within what flush() left of the timeout.
            thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        closer = getattr(self.send, "close", None)
        if closer:
            closer()

    def _after_fork(self) -> None:
        self._cond = threading.Condition()
        self._thread = None
        self._busy = self._stopping = self._kicked = False
        # A lock another thread held at the fork stays held in the child:
        # the core's are made anew (the queue empty: the parent sends it).
        self.core.after_fork()
        self.delivery = Delivery()
        reset = getattr(self.send, "after_fork", None)
        if reset:
            reset()
        self._spool_loaded = False
        if self.core.spool is not None:
            self.core.spool.reopen()
