"""The offline spool: requests survive outages and restarts."""

import asyncio
import os
import stat
import time

import pytest

from fixwire import AsyncClient, Client
from fixwire._core.delivery import Outbound
from fixwire.drivers import spool as spool_mod
from fixwire.drivers.spool import Spool


def test_spool_caps_ttl_and_claims(tmp_path, monkeypatch):
    s = Spool(str(tmp_path / "s.db"), max_items=3)
    ids = [s.put(Outbound("/v1/logs", "application/json", b"x%d" % i, "error")) for i in range(5)]
    assert s.count() == 3  # the oldest went
    rows = s.claim()
    assert [r.spool_id for r in rows] == ids[2:]
    assert (rows[0].path, rows[0].content_type, rows[0].body, rows[0].category) == (
        "/v1/logs",
        "application/json",
        b"x2",
        "error",
    )
    s.delete(ids[2])
    assert s.count() == 2

    # Rows another live process holds are not taken; stale claims are.
    s._db.execute("UPDATE requests SET owner = 1, claimed = ?", (time.time(),))
    assert s.claim() == []
    s._db.execute("UPDATE requests SET claimed = ?", (time.time() - spool_mod.RECLAIM_AFTER - 1,))
    assert len(s.claim()) == 2

    # Expired rows go.
    s._db.execute("UPDATE requests SET created = ?", (time.time() - spool_mod.TTL_SECONDS - 1,))
    assert s.claim() == [] and s.count() == 0


def test_an_outage_and_a_restart_lose_nothing(ingest, tmp_path, monkeypatch):
    import fixwire._core.delivery as delivery

    monkeypatch.setattr(delivery, "BACKOFF_BASE", 0.05)
    path = str(tmp_path / "spool.db")
    ingest.responses = [(503, {})] * 100  # the server is down

    first = Client(ingest.dsn, offline=path, default_integrations=False)
    first.capture_message("written during the outage")
    first.flush(0.5)
    first.close(0.2)
    assert Spool(path).count() == 1

    # The server is back; a new process starts and delivers what was left.
    ingest.responses = []
    ingest.requests.clear()
    second = Client(ingest.dsn, offline=path, default_integrations=False)
    assert ingest.wait(1) >= 1
    assert second.flush(5)
    second.close()
    assert "written during the outage" in [e.get("message") for e in ingest.events()]
    assert ingest.requests[0]["path"] == "/v1/logs"
    assert Spool(path).count() == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_spool_is_readable_by_its_user_only(tmp_path):
    path = tmp_path / "cache" / "spool.db"
    Spool(str(path)).put(Outbound("/v1/logs", "application/json", b"x", "error"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    wal = path.with_name("spool.db-wal")
    assert not wal.exists() or stat.S_IMODE(os.stat(wal).st_mode) == 0o600


def test_requests_given_up_on_leave_the_spool(tmp_path, monkeypatch):
    import fixwire._core.delivery as delivery

    monkeypatch.setattr(delivery, "MAX_ATTEMPTS", 1)
    path = str(tmp_path / "spool.db")

    async def main():
        # Nothing listens there: the one attempt fails and the request is dropped.
        async with AsyncClient("http://k@127.0.0.1:9", offline=path, default_integrations=False) as client:
            client.capture_message("given up")
            assert await client.aflush(5)

    asyncio.run(main())
    assert Spool(path).count() == 0


def test_off_by_default(ingest):
    c = Client(ingest.dsn, default_integrations=False)
    assert c.core.spool is None
    c.close()
