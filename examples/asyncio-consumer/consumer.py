"""An asyncio worker that consumes order events from a queue.

Uses AsyncClient: delivery runs on the event loop and never blocks it, and
each message gets its own scope so tags never leak between messages.

    FIXWIRE_DSN=https://<key>@<host> python consumer.py
"""

from __future__ import annotations

import asyncio
import os
from typing import TypedDict

import fixwire


class Order(TypedDict):
    id: str
    amount: int


async def handle(message: Order) -> None:
    await asyncio.sleep(0.01)  # some I/O
    if message["amount"] <= 0:
        raise ValueError("order %s has a non-positive amount" % message["id"])


async def consume(client: fixwire.AsyncClient, queue: asyncio.Queue[Order | None]) -> None:
    while (message := await queue.get()) is not None:
        with fixwire.isolation_scope() as scope:
            scope.set_tag("order", message["id"])
            scope.set_context("message", dict(message))
            try:
                await handle(message)
            except Exception:
                client.capture_exception()
        queue.task_done()


async def main() -> None:
    async with fixwire.AsyncClient(
        os.environ.get("FIXWIRE_DSN"), release="orders-consumer@1.0.0", default_integrations=False
    ) as client:
        queue: asyncio.Queue[Order | None] = asyncio.Queue()
        for i, amount in enumerate([25, 0, 13, -4]):
            queue.put_nowait({"id": "ord_%d" % i, "amount": amount})
        workers = [asyncio.create_task(consume(client, queue)) for _ in range(2)]
        await queue.join()
        for _ in workers:
            queue.put_nowait(None)
        await asyncio.gather(*workers)
        await client.aflush()  # leaving the block also flushes


if __name__ == "__main__":
    asyncio.run(main())
