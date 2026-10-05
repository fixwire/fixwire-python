"""Apps that already run OpenTelemetry keep it: Fixwire never sets a tracer
provider, propagator or context. ``OpenTelemetryIntegration`` puts the
active OpenTelemetry span's trace on Fixwire's errors when no Fixwire span
is active, and ``otlp_exporter_options(dsn)`` points the app's own OTLP
exporters at Fixwire::

    from fixwire.integrations.opentelemetry import OpenTelemetryIntegration, otlp_exporter_options

    fixwire.init(dsn, integrations=[OpenTelemetryIntegration()])
    exporter = OTLPSpanExporter(**otlp_exporter_options(dsn)["traces"])

The integration needs ``opentelemetry-api``; without it, it does nothing.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import TYPE_CHECKING, Any, TypedDict

from fixwire._core.dsn import Dsn
from fixwire._core.scope import get_global_scope
from fixwire._core.tracing import current_span
from fixwire.integrations import install_once

if TYPE_CHECKING:
    from fixwire.client import Client
    from fixwire.types import Event, Hint

__all__ = ["OpenTelemetryIntegration", "OtlpTarget", "otlp_exporter_options"]

logger = logging.getLogger("fixwire")


class OpenTelemetryIntegration:
    def setup(self, client: Client) -> None:
        if not install_once("opentelemetry"):
            return
        if importlib.util.find_spec("opentelemetry") is None:
            logger.warning("fixwire: OpenTelemetryIntegration needs opentelemetry-api")
            return
        get_global_scope().add_event_processor(_link)


def _link(event: Event, hint: Hint) -> Event | None:
    """Puts the active OpenTelemetry span's trace on the event, unless a
    Fixwire span is active."""
    if current_span() is not None:
        return event
    from opentelemetry import trace

    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return event
    contexts: dict[str, Any] = dict(event.get("contexts") or {})
    contexts["trace"] = {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
    event["contexts"] = contexts
    return event


class OtlpTarget(TypedDict):
    """Keyword arguments of an OTLP/HTTP exporter (``OTLPSpanExporter``, ``OTLPLogExporter``)."""

    endpoint: str
    headers: dict[str, str]


class OtlpTargets(TypedDict):
    traces: OtlpTarget
    logs: OtlpTarget


def otlp_exporter_options(dsn: str) -> OtlpTargets:
    """OTLP/HTTP exporter options for a DSN: traces and logs, with the key as a bearer token."""
    d = Dsn.parse(dsn)
    headers = {"Authorization": d.auth_header()}
    return {
        "traces": {"endpoint": d.url("/v1/traces"), "headers": dict(headers)},
        "logs": {"endpoint": d.url("/v1/logs"), "headers": dict(headers)},
    }
