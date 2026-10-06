"""Log records as breadcrumbs (INFO and up) and as events (ERROR and up),
through a handler on the root logger: nothing is patched."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from fixwire.integrations import current_client, install_once
from fixwire.types import Hint, Level

if TYPE_CHECKING:
    from fixwire.client import Client

_LEVELS: dict[int, Level] = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "fatal",
}
_IGNORED = ("fixwire", "urllib3.connectionpool", "httpx", "httpcore")
#: Set while a record is reported: what before_send, an event processor or
#: before_breadcrumb log meanwhile isn't reported again (no recursion).
_reporting: ContextVar[bool] = ContextVar("fixwire_logging_reporting", default=False)


def _level(levelno: int) -> Level:
    for threshold in (logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO):
        if levelno >= threshold:
            return _LEVELS[threshold]
    return "debug"


class FixwireHandler(logging.Handler):
    def __init__(self, breadcrumb_level: int, event_level: int) -> None:
        super().__init__(level=min(breadcrumb_level, event_level))
        self.breadcrumb_level, self.event_level = breadcrumb_level, event_level

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_IGNORED) or _reporting.get():
            return
        client = current_client()
        if client is None:
            return
        token = _reporting.set(True)
        try:
            if record.levelno >= self.event_level:
                self._event(client, record)
            elif record.levelno >= self.breadcrumb_level:
                import fixwire

                fixwire.add_breadcrumb(
                    category=record.name,
                    level=_level(record.levelno),
                    message=record.getMessage(),
                    timestamp=record.created,
                    type="log",
                )
        except Exception:
            self.handleError(record)
        finally:
            _reporting.reset(token)

    def _event(self, client: Client, record: logging.LogRecord) -> None:
        event: dict[str, Any]
        hint: Hint
        exc = record.exc_info[1] if record.exc_info else None
        if record.exc_info and exc is not None:
            if getattr(exc, "__fixwire_captured__", False):
                return
            event, hint = client.core.event_from_exception(exc, {"type": "logging", "handled": True})
        else:
            event, hint = {}, {}
        event["level"] = _level(record.levelno)
        event["logger"] = record.name
        msg = record.msg if isinstance(record.msg, str) else str(record.msg)
        logentry: dict[str, Any] = {"message": msg, "formatted": record.getMessage()}
        if record.args and isinstance(record.args, tuple):
            logentry["params"] = [str(a) for a in record.args]
        event["logentry"] = logentry
        hint["log_record"] = record
        event_id = client.capture_event(event, hint)
        if event_id and exc is not None:
            try:
                setattr(exc, "__fixwire_captured__", True)  # noqa: B010
            except Exception:
                pass


class LoggingIntegration:
    def __init__(self, level: int = logging.INFO, event_level: int = logging.ERROR) -> None:
        self.level, self.event_level = level, event_level

    def setup(self, client: Client) -> None:
        if not install_once("logging"):
            return
        logging.getLogger().addHandler(FixwireHandler(self.level, self.event_level))
