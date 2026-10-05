"""A customer-support agent on Claude, traced with Fixwire.

Each question is one agent run in Fixwire: its model calls (model, tokens,
cache tokens, stop reason) and tool calls (arguments hash, failures by
error class), with totals and cost per run.

    ANTHROPIC_API_KEY=... FIXWIRE_DSN=https://<key>@<host> python agent.py "Refund ord_1, it arrived broken"

Without FIXWIRE_DSN the agent still works; nothing is sent.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import fixwire

if TYPE_CHECKING:
    from anthropic import Anthropic
    from anthropic.types import MessageParam, ToolParam, ToolResultBlockParam

# Any Claude model works; this is just the example's default.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
MAX_STEPS = 8

fixwire.init(
    dsn=os.environ.get("FIXWIRE_DSN"),
    release=os.environ.get("RELEASE", "support-agent@1.0.0"),
    environment=os.environ.get("ENVIRONMENT", "development"),
    traces_sample_rate=1.0,
    # Prompts and answers stay out of Fixwire unless this is on (they're
    # redacted on this machine when it is).
    record_ai_content=os.environ.get("RECORD_AI_CONTENT") == "1",
)

ORDERS: dict[str, dict[str, str]] = {
    "ord_1": {"status": "shipped", "carrier": "DHL", "eta": "2026-10-06", "total": "49.00 EUR"},
    "ord_2": {"status": "processing", "total": "14.90 EUR"},
}

SYSTEM = (
    "You are the support agent of a small coffee shop. Look orders up before answering. "
    "Refunds are possible only before an order ships; explain that kindly when it has."
)

TOOLS: list[ToolParam] = [
    {
        "name": "lookup_order",
        "description": "Look up an order's status, carrier and total by its id.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "The order id, e.g. ord_1."}},
            "required": ["order_id"],
        },
    },
    {
        "name": "refund_order",
        "description": "Refund an order that has not shipped yet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The order id, e.g. ord_2."},
                "reason": {"type": "string", "description": "Why the customer wants a refund."},
            },
            "required": ["order_id", "reason"],
        },
    },
]


class ToolError(Exception):
    """A tool refused: the model is told and can recover."""


class OrderNotFound(ToolError):
    pass


class AlreadyShipped(ToolError):
    pass


def lookup_order(order_id: str) -> dict[str, str]:
    order = ORDERS.get(order_id)
    if order is None:
        raise OrderNotFound("no order %s" % order_id)
    return order


def refund_order(order_id: str, reason: str) -> dict[str, str]:
    order = lookup_order(order_id)
    if order["status"] == "shipped":
        raise AlreadyShipped("order %s has shipped; refunds are possible only before shipping" % order_id)
    order["status"] = "refunded"
    return {"order_id": order_id, "status": "refunded", "reason": reason}


HANDLERS: dict[str, Callable[..., dict[str, str]]] = {"lookup_order": lookup_order, "refund_order": refund_order}


def answer(client: Anthropic, question: str) -> str:
    """Runs the agent on one question and returns its reply."""
    with fixwire.ai.agent("support-agent", provider="anthropic", model=MODEL, input=question) as run:
        messages: list[MessageParam] = [{"role": "user", "content": question}]
        reply = ""
        for _ in range(MAX_STEPS):
            # A chat span per call: wrap_anthropic() records model, tokens and stop reason.
            response = client.messages.create(
                model=MODEL, max_tokens=4096, system=SYSTEM, tools=TOOLS, messages=messages
            )
            if response.stop_reason != "tool_use":
                reply = "".join(b.text for b in response.content if b.type == "text")
                break
            messages.append({"role": "assistant", "content": response.content})
            results: list[ToolResultBlockParam] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                args = cast("dict[str, Any]", block.input)
                try:
                    # An execute_tool span; a ToolError marks it failed with its class.
                    with fixwire.ai.tool(block.name, call_id=block.id, arguments=args) as call:
                        result = HANDLERS[block.name](**args)
                        call.set_result(result)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                except ToolError as e:
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True}
                    )
            messages.append({"role": "user", "content": results})
        else:
            run.span.set_attribute("gen_ai.agent.max_steps_reached", True)
        run.set_output(reply)
        return reply


def main() -> None:
    from anthropic import Anthropic

    client = fixwire.ai.wrap_anthropic(Anthropic())
    question = " ".join(sys.argv[1:]) or "Where is my order ord_1?"
    print(answer(client, question))
    fixwire.flush()


if __name__ == "__main__":
    main()
