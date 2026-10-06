"""The asyncio driver: a task on the owning loop and up to four requests in
flight, never a task per request. Captures from any thread append to the
core's queue and wake the loop thread-safely; encoding (serialize, redact,
gzip, reading source lines) runs on one helper thread, so the loop is never
blocked by our work.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fixwire._core.delivery import Delivery, Outbound
from fixwire._core.pipeline import Core

logger = logging.getLogger("fixwire")

MAX_IN_FLIGHT = 4


class AsyncioDriver:
    def __init__(
        self,
        core: Core,
        send: Callable[[str, bytes, dict[str, str]], Awaitable[tuple[int, dict[str, str]]]],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.core = core
        self.send = send
        self.loop = loop or asyncio.get_running_loop()
        self.delivery = Delivery(max_items=core.options.max_queue_size)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._in_flight: set[Any] = set()
        self._sem = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._encoder = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="fixwire-encode")
        self._closed = False
        self._spool_loaded = False

    @property
    def usable(self) -> bool:
        return not self._closed and not self.loop.is_closed()

    def submit(self, event: dict[str, Any]) -> bool:
        """Queues an event from any thread; False if the loop is gone."""
        if not self.usable:
            return False
        self.core.queue.put(event)
        try:
            if _running_loop() is self.loop:
                self._kick()
            else:
                self.loop.call_soon_threadsafe(self._kick)
        except RuntimeError:  # the loop closed meanwhile
            return False
        return True

    def _kick(self) -> None:
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = self.loop.create_task(self._run(), name="fixwire-delivery")

    def _pending(self) -> bool:
        return bool(len(self.core.queue)) or not self.delivery.empty() or bool(self._in_flight)

    async def _run(self) -> None:
        while not self._closed:
            self._wake.clear()
            try:
                await self._step()
            except Exception:
                logger.exception("fixwire: delivery failed")
            wake = self.delivery.wake_at()
            timeout = None if wake is None else max(0.0, wake - time.monotonic())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    def _only_waiting(self) -> bool:
        wake = self.delivery.wake_at()
        return wake is not None and wake > time.monotonic()

    async def _step(self) -> None:
        if not self._spool_loaded:
            self._spool_loaded = True
            for out in await self.loop.run_in_executor(self._encoder, self.core.spool_load):
                self.delivery.offer(out, time.monotonic())
        events = self.core.queue.drain()
        if events:
            encoded = await self.loop.run_in_executor(self._encoder, _encode_all, self.core, events)
            now = time.monotonic()
            for out in encoded:
                self.delivery.offer(out, now)
        while True:
            item = self.delivery.next(time.monotonic())
            if item is None:
                break
            await self._sem.acquire()
            task = self.loop.create_task(self._send(item))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)

    async def _send(self, item: Outbound) -> None:
        try:
            try:
                status, headers = await self.send(self.core.url(item), item.body, self.core.headers(item))
            except Exception as e:
                d = self.delivery.on_error(item, time.monotonic(), type(e).__name__)
            else:
                d = self.delivery.on_response(item, status, headers, time.monotonic())
            if not d.retry and item.spool_id is not None:
                await self.loop.run_in_executor(self._encoder, self.core.spool_done, item)
            if d.dropped and self.core.options.debug:
                logger.warning("fixwire: dropped a request to %s (%s)", item.path, d.reason)
        finally:
            self._sem.release()
            self._wake.set()

    async def aflush(self, timeout: float | None = None) -> bool:
        if self._pending():
            self._kick()
        try:
            await asyncio.wait_for(self._drained(), timeout)
            return not self._pending()
        except asyncio.TimeoutError:
            return False

    async def _drained(self) -> None:
        while self._pending():
            if not len(self.core.queue) and not self._in_flight and self._only_waiting():
                return  # only backoff waits remain
            self._wake.set()
            await asyncio.sleep(0.01)
            if self._in_flight:
                await asyncio.wait(set(self._in_flight))

    async def aclose(self, timeout: float | None = None) -> None:
        await self.aflush(timeout)
        self._closed = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        closer = getattr(self.send, "aclose", None)
        if closer:
            await closer()
        self._encoder.shutdown(wait=False)

    def take_leftovers(self) -> list[Outbound]:
        """Encoded requests not yet sent, for a fallback driver once the
        loop is gone."""
        return self.delivery.take()


def _encode_all(core: Core, events: list[Any]) -> list[Outbound]:
    out: list[Outbound] = []
    for e in events:
        for enc in core.encode(e):
            core.spool_put(enc)
            out.append(enc)
    return out


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
