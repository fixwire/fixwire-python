"""Integrations are explicit and install once per process. They look up the
current client when they fire, so re-initializing the SDK needs no
reinstall. The defaults patch nothing in other libraries: they use the
interpreter's own hooks (sys.excepthook, threading.excepthook, atexit) and
a logging handler."""

from __future__ import annotations

import atexit
import logging
import sys
import threading
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fixwire.client import Client

_installed: set[str] = set()


def install_once(name: str) -> bool:
    """True the first time it's called with this name in the process: an
    integration installs its hooks only then."""
    if name in _installed:
        return False
    _installed.add(name)
    return True


def current_client() -> Client | None:
    import fixwire

    return fixwire.get_client()


def install_defaults(client: Client) -> None:
    from fixwire.integrations.logging import LoggingIntegration

    for integration in (ExcepthookIntegration(), AtexitIntegration(), LoggingIntegration()):
        integration.setup(client)


class ExcepthookIntegration:
    """Uncaught exceptions in the main thread and in threads, reported as
    unhandled; the original hooks still run."""

    def setup(self, client: Client) -> None:
        if not install_once("excepthook"):
            return
        previous = sys.excepthook

        def excepthook(exc_type: type[BaseException], exc_value: BaseException, tb: TracebackType | None) -> None:
            c = current_client()
            if c is not None and not issubclass(exc_type, KeyboardInterrupt):
                try:
                    c.capture_exception((exc_type, exc_value, tb), mechanism={"type": "excepthook", "handled": False})
                    c.flush()
                except Exception:
                    pass
            previous(exc_type, exc_value, tb)

        sys.excepthook = excepthook
        previous_thread = threading.excepthook

        def thread_excepthook(args: threading.ExceptHookArgs) -> None:
            c = current_client()
            if c is not None and args.exc_type is not SystemExit and args.exc_value is not None:
                try:
                    c.capture_exception(
                        (args.exc_type, args.exc_value, args.exc_traceback),
                        mechanism={"type": "threading", "handled": False},
                    )
                except Exception:
                    pass
            previous_thread(args)

        threading.excepthook = thread_excepthook


class AtexitIntegration:
    """Sends what is queued when the process exits (up to shutdown_timeout)."""

    def setup(self, client: Client) -> None:
        if not install_once("atexit"):
            return

        def flush_at_exit() -> None:
            c = current_client()
            if c is None:
                return
            try:
                c.close()
            except Exception:
                logging.getLogger("fixwire").debug("fixwire: flush at exit failed", exc_info=True)

        atexit.register(flush_at_exit)
