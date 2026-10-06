"""Release health: sessions. Each request a server handles (and each
serverless invocation) is one: exited, errored (a handled error was
reported) or crashed (an unhandled one). Requests are counted per minute and
user and sent as aggregates about every minute, and on flush. Sessions need
a release, and send no content: their user is a hash of the user's id,
email or username, made on this machine."""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from typing import Any

#: Seconds between sends of request sessions while running.
INTERVAL = 60.0
#: Users counted apart until a send; past it, requests count without their
#: user, so a send stays under the ingest's 1 MB.
MAX_USERS = 5000
#: Aggregates per /v1/sessions request.
MAX_AGGREGATES = 5000

_COLUMNS = {"ok": 0, "errored": 1, "crashed": 2}


class RequestSession:
    """The session of the request an isolation scope serves."""

    __slots__ = ("status",)

    def __init__(self) -> None:
        #: "ok", "errored" or "crashed".
        self.status = "ok"


def identity(user: Any | None) -> str | None:
    """Who a session belongs to, before hashing."""
    if not user:
        return None
    for key in ("id", "email", "username"):
        value = user.get(key)
        if value is not None and value != "":
            return str(value)
    return None


def hash_identity(value: str | None) -> str | None:
    """A hash of an identity (32 hex), made here so it never leaves as is."""
    return None if value is None else hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


class Aggregates:
    """Request sessions counted per minute and user, until sent."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[tuple[int, str | None], list[int]] = {}
        self._users: set[str] = set()
        self._since: float | None = None
        self._due = False

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)

    def after_fork(self) -> None:
        self._lock = threading.Lock()

    def record(self, status: str, user: str | None, at: float) -> bool:
        """Counts one request; True the first time a send is due."""
        did = hash_identity(user)
        with self._lock:
            if did is not None and did not in self._users:
                if len(self._users) >= MAX_USERS:
                    did = None  # counted, without its user
                else:
                    self._users.add(did)
            self._buckets.setdefault((int(at // 60) * 60, did), [0, 0, 0])[_COLUMNS.get(status, 0)] += 1
            if self._since is None:
                self._since = at
            if not self._due and at - self._since >= INTERVAL:
                self._due = True
                return True
        return False

    def take(self) -> list[dict[str, Any]] | None:
        """Empties the counts into the ``aggregates`` of a /v1/sessions body."""
        with self._lock:
            buckets, self._buckets = self._buckets, {}
            self._users = set()
            self._since, self._due = None, False
        if not buckets:
            return None
        aggregates: list[dict[str, Any]] = []
        for (minute, did), (exited, errored, crashed) in sorted(
            buckets.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
        ):
            a: dict[str, Any] = {"started": datetime.fromtimestamp(minute, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            if did:
                a["did"] = did
            if exited:
                a["exited"] = exited
            if errored:
                a["errored"] = errored
            if crashed:
                a["crashed"] = crashed
            aggregates.append(a)
        return aggregates
