"""AI agent tracing: gen_ai spans, the Anthropic wrapper (sync, async,
streamed), content recording and the tool-arguments hash."""

import asyncio
from types import SimpleNamespace

import pytest

import fixwire
from fixwire import ai


def span_items(ingest):
    return ingest.spans()


def attr(span, key):
    return span["attributes"].get(key)


def by_op(spans, op):
    return [s for s in spans if attr(s, "fixwire.op") == op]


MESSAGE = SimpleNamespace(
    id="msg_1",
    model="claude-opus-5-5-20261001",
    stop_reason="tool_use",
    content=[SimpleNamespace(type="tool_use", id="toolu_1", name="lookup_order", input={"id": "ord_1"})],
    usage=SimpleNamespace(
        input_tokens=1200, output_tokens=80, cache_read_input_tokens=1000, cache_creation_input_tokens=0
    ),
)
EVENTS = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_2",
            "model": "claude-opus-5-5-20261001",
            "usage": {"input_tokens": 900, "output_tokens": 1},
        },
    },
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Your order "}},
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ships today."}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
]


class FakeMessages:
    def create(self, **params):
        if params["model"] == "broken":
            raise ConnectionError("overloaded")
        return iter(EVENTS) if params.get("stream") else MESSAGE


class FakeAsyncMessages:
    async def create(self, **params):
        if params["model"] == "broken":
            raise ConnectionError("overloaded")
        if params.get("stream"):

            async def events():
                for ev in EVENTS:
                    yield ev

            return events()
        return MESSAGE


def test_an_agent_run_with_model_and_tool_calls(ingest):
    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False)
    anthropic = ai.wrap_anthropic(SimpleNamespace(messages=FakeMessages(), api_key="untouched"))
    assert anthropic.api_key == "untouched"
    with ai.agent("support-bot", provider="anthropic", model="claude-opus-5-5", input="where is my order?") as run:
        msg = anthropic.messages.create(
            model="claude-opus-5-5", max_tokens=1024, messages=[{"role": "user", "content": "where?"}]
        )
        block = msg.content[0]
        with ai.tool(block.name, call_id=block.id, arguments=block.input) as t:
            t.set_result({"status": "shipped"})
        with pytest.raises(OverflowError):
            with ai.tool("refund", arguments={"id": "ord_1", "amount": 5}):
                raise OverflowError("amount above the limit")
        text = ""
        for ev in anthropic.messages.create(model="claude-opus-5-5", max_tokens=1024, stream=True, messages=[]):
            if ev["type"] == "content_block_delta":
                text += ev["delta"]["text"]
        run.set_output(text)
    assert fixwire.flush(5)

    spans = span_items(ingest)
    [agent] = by_op(spans, "gen_ai.invoke_agent")
    assert agent["name"] == "invoke_agent support-bot" and agent["is_segment"]
    assert attr(agent, "gen_ai.input.messages") is None and attr(agent, "gen_ai.output.messages") is None
    plain, streamed = by_op(spans, "gen_ai.chat")
    for s in (plain, streamed):
        assert s["parent_span_id"] == agent["span_id"] and s["name"] == "chat claude-opus-5-5"
        assert attr(s, "gen_ai.provider.name") == "anthropic" and attr(s, "gen_ai.agent.name") == "support-bot"
        assert attr(s, "gen_ai.response.model") == "claude-opus-5-5-20261001"
        assert attr(s, "fixwire.origin") == "auto.ai.anthropic"
    assert attr(plain, "gen_ai.usage.input_tokens") == 2200  # cache reads included
    assert attr(plain, "gen_ai.usage.cache_read.input_tokens") == 1000
    assert attr(plain, "gen_ai.response.finish_reasons") == '["tool_use"]'
    assert attr(streamed, "gen_ai.response.id") == "msg_2"
    assert attr(streamed, "gen_ai.usage.input_tokens") == 900 and attr(streamed, "gen_ai.usage.output_tokens") == 42
    assert attr(streamed, "gen_ai.response.finish_reasons") == '["end_turn"]'
    tools = {s["name"]: s for s in by_op(spans, "gen_ai.execute_tool")}
    lookup, refund = tools["execute_tool lookup_order"], tools["execute_tool refund"]
    assert attr(lookup, "gen_ai.tool.call.id") == "toolu_1"
    assert attr(lookup, "fixwire.tool.arguments_hash") == ai.arguments_hash({"id": "ord_1"})
    assert attr(lookup, "gen_ai.tool.call.arguments") is None and attr(lookup, "gen_ai.tool.call.result") is None
    assert refund["status"] == "error" and attr(refund, "error.type") == "OverflowError"
    assert attr(refund, "gen_ai.agent.name") == "support-bot"


def test_the_async_client(ingest):
    async def main():
        fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False, transport="thread")
        anthropic = ai.wrap_anthropic(SimpleNamespace(messages=FakeAsyncMessages()))
        with ai.agent("async-bot"):
            msg = await anthropic.messages.create(model="claude-opus-5-5", max_tokens=10, messages=[])
            assert msg is MESSAGE
            stream = await anthropic.messages.create(model="claude-opus-5-5", max_tokens=10, stream=True, messages=[])
            async for _ in stream:
                pass
            with pytest.raises(ConnectionError):
                await anthropic.messages.create(model="broken", max_tokens=10, messages=[])

    asyncio.run(main())
    assert fixwire.flush(5)
    chats = by_op(span_items(ingest), "gen_ai.chat")
    assert len(chats) == 3
    assert {attr(s, "gen_ai.usage.output_tokens") for s in chats if s["status"] == "ok"} == {80, 42}
    broken = next(s for s in chats if s["name"] == "chat broken")
    assert broken["status"] == "error" and attr(broken, "error.type") == "ConnectionError"


def test_content_is_recorded_only_when_asked_bounded_and_redacted(ingest):
    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False, record_ai_content=True)
    with ai.agent("triage", input={"email": "ada@example.com", "text": "refund please"}) as run:
        with ai.chat("anthropic", "claude-opus-5-5", input=[{"role": "user", "content": "x" * 40_000}]) as call:
            call.set_response(output=[{"role": "assistant", "content": "card 4111 1111 1111 1111 refunded"}])
        with ai.tool("notify", arguments={"to": "ada@example.com"}, record_content=False):
            pass
        run.set_output("done")
    assert fixwire.flush(5)
    spans = span_items(ingest)
    [agent], [chat], [notify] = (
        by_op(spans, "gen_ai.invoke_agent"),
        by_op(spans, "gen_ai.chat"),
        by_op(spans, "gen_ai.execute_tool"),
    )
    assert "refund please" in attr(agent, "gen_ai.input.messages") and "ada@example.com" not in attr(
        agent, "gen_ai.input.messages"
    )
    assert attr(agent, "gen_ai.output.messages") == "done"
    assert len(attr(chat, "gen_ai.input.messages")) <= ai.MAX_AI_CONTENT
    assert "4111" not in attr(chat, "gen_ai.output.messages")
    assert attr(notify, "gen_ai.tool.call.arguments") is None


def test_argument_hashes_match_the_javascript_sdk():
    assert ai.arguments_hash({"id": "ord_1"}) == "e665776feba25695"
    assert ai.arguments_hash({"b": 2, "a": [1, {"y": "é", "x": True}]}) == "d12b766c8a7a2cc0"
    assert ai.arguments_hash({"a": [1, {"x": True, "y": "é"}], "b": 2}) == "d12b766c8a7a2cc0"


def test_an_abandoned_stream_still_ends_its_span(ingest):
    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False)
    anthropic = ai.wrap_anthropic(SimpleNamespace(messages=FakeMessages()))
    with fixwire.start_span("job"):
        for _ in anthropic.messages.create(model="claude-opus-5-5", max_tokens=1, stream=True, messages=[]):
            break
    assert fixwire.flush(5)
    [chat] = by_op(span_items(ingest), "gen_ai.chat")
    assert attr(chat, "gen_ai.response.id") == "msg_2"
