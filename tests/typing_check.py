"""The public API as users write it, checked by mypy and pyright (strict)
but never run: if a signature loses its types, CI fails here."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import fixwire
from fixwire.types import Breadcrumb, Event, Hint, SamplingContext


def before_send(event: Event, hint: Hint) -> Event | None:
    if event.get("logger") == "noisy":
        return None
    exc_info = hint.get("exc_info")
    if exc_info is not None and isinstance(exc_info[1], ConnectionResetError):
        return None
    event.setdefault("tags", {})["checked"] = "yes"
    return event


def sampler(context: SamplingContext) -> float:
    return 0.0 if context["name"].startswith("GET /health") else 0.2


def before_breadcrumb(crumb: Breadcrumb, hint: dict[str, object]) -> Breadcrumb | None:
    return None if crumb.get("category") == "noise" else crumb


client: fixwire.Client = fixwire.init(
    "https://fw_pk_live_key@ingest.eu.fixwire.io",
    release="api@1.0.0",
    traces_sample_rate=0.2,
    traces_sampler=sampler,
    before_send=before_send,
    before_breadcrumb=before_breadcrumb,
    trace_propagation_targets=["api.internal.example"],
    rate_limit={"per_issue_burst": 5},
    offline=True,
)

# A misspelled option is a type error (strict mode reports unused ignores, so
# this line fails CI if the typo stops being caught).
fixwire.init("https://key@ingest.example", releas="typo")  # type: ignore[call-arg]

event_id: str | None = fixwire.capture_message("hello", level="warning")
feedback_id: str | None = fixwire.capture_feedback(
    "wrong answer", score=-1, trace_id="0af7651916cd43dd8448eb211c80319c"
)
fixwire.capture_feedback("x", -1)  # type: ignore[call-arg]
run_id: str | None = fixwire.capture_check_in(
    "nightly-report",
    "in_progress",
    monitor_config={"schedule": {"type": "crontab", "value": "0 3 * * *"}, "checkin_margin": 5, "timezone": "UTC"},
)
fixwire.capture_check_in("nightly-report", "ok", check_in_id=run_id, duration=42.5)
fixwire.capture_check_in("nightly-report", "late")  # type: ignore[arg-type]
fixwire.add_breadcrumb(category="cart", message="added sku-1", level="info")
with fixwire.start_span("job", op="task") as span:
    span.set_attribute("items", 3)
headers: dict[str, str] = fixwire.trace_headers()


async def main() -> None:
    async with fixwire.AsyncClient("https://key@ingest.example", release="worker@1.0.0") as c:
        c.capture_exception(ValueError("boom"))
        ok: bool = await c.aflush()
        assert ok


def drop_health(event: Event, hint: Hint) -> Event | None:
    return None if event.get("transaction") == "GET /health" else event


fixwire.set_user({"id": 42, "email": "ada@example.com"})
fixwire.set_user({"idd": 42})  # type: ignore[arg-type]
fixwire.set_tags({"tenant": "acme", "plan": "pro"})
fixwire.capture_message("x", level="warn")  # type: ignore[arg-type]
fixwire.add_breadcrumb(categoryy="typo")  # type: ignore[call-arg]
fixwire.capture_event({"message": "built by hand", "level": "info", "tags": {"source": "cron"}})

with fixwire.isolation_scope() as scope:
    scope.set_tag("tenant", "acme")
    scope.set_level("warning")
    scope.set_fingerprint(["payments", "timeout"])
    scope.add_event_processor(drop_health)
    fixwire.continue_trace({"traceparent": "00-" + "1" * 32 + "-" + "1" * 16 + "-01", "tracestate": "vendor=a"})
    with fixwire.start_span("charge", op="payment", attributes={"amount": 12.5}) as charge:
        charge.set_status("error")
        charge.set_status("broken")  # type: ignore[arg-type]


def plain_before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Callbacks typed with plain dicts are accepted too."""
    return event


fixwire.init(
    "https://key@ingest.example",
    before_send=plain_before_send,
    traces_sampler=lambda ctx: 1.0 if ctx["parent_sampled"] else 0.1,
)


# AI agent tracing.
class _FakeAnthropicMessages:
    def create(self, *, model: str, max_tokens: int) -> dict[str, Any]:
        return {"id": "m", "model": model, "max_tokens": max_tokens}


class _FakeAnthropic:
    messages = _FakeAnthropicMessages()


class _FakeOpenAICompletions:
    def create(self, *, model: str) -> dict[str, Any]:
        return {"id": "c", "model": model}


class _FakeOpenAIChat:
    completions = _FakeOpenAICompletions()


class _FakeOpenAI:
    chat = _FakeOpenAIChat()


anthropic_client: _FakeAnthropic = fixwire.ai.wrap_anthropic(_FakeAnthropic())
openai_client: _FakeOpenAI = fixwire.ai.wrap_openai(_FakeOpenAI())
completion: dict[str, Any] = openai_client.chat.completions.create(model="gpt-x")
with fixwire.ai.agent("support-bot", provider="anthropic", model="claude-opus-5-5", conversation_id="c-1") as agent_run:
    reply: dict[str, Any] = anthropic_client.messages.create(model="claude-opus-5-5", max_tokens=512)
    with fixwire.ai.tool("lookup_order", call_id="toolu_1", arguments={"id": "ord_1"}) as tool_call:
        tool_call.set_result({"status": "shipped"})
    with fixwire.ai.chat("openai", "gpt-x", max_tokens=100) as chat_call:
        chat_call.set_response(finish_reasons=["stop"], usage={"input_tokens": 10, "output_tokens": 2})
        chat_call.set_usage(input_tokens=10, outputt_tokens=2)  # type: ignore[call-arg]
    agent_run.set_output("done")
digest: str = fixwire.ai.arguments_hash({"id": "ord_1"})
fixwire.init("https://key@ingest.example", record_ai_content=True)


# Serverless handlers keep their signatures.
@fixwire.serverless_function
def lambda_handler(event: dict[str, Any], context: object) -> dict[str, int]:
    return {"status": 200}


@fixwire.serverless_function(name="report", flush_timeout=1)
async def async_handler(event: dict[str, Any]) -> str:
    return "done"


status: dict[str, int] = lambda_handler({}, object())
pending: Coroutine[Any, Any, str] = async_handler({})
lambda_handler("not a dict", object())  # type: ignore[arg-type]
