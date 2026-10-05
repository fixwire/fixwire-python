"""The provider wrappers against the real Anthropic and OpenAI SDK clients,
sync and async, with their HTTP answered locally."""

import asyncio
import json

import anthropic
import httpx
import httpx2  # the Anthropic SDK's HTTP client
import openai

import fixwire
from fixwire import ai


def attr(span, key):
    return span["attributes"].get(key)


def chats(ingest, op="gen_ai.chat"):
    return [s for s in ingest.spans() if attr(s, "fixwire.op") == op]


def sse(*events, http=httpx):
    body = "".join(
        ("event: %s\n" % name if name else "") + "data: %s\n\n" % (data if isinstance(data, str) else json.dumps(data))
        for name, data in events
    )
    return http.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-x-20261001",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 0},
}


def anthropic_answer(request):
    if json.loads(request.content).get("stream"):
        return sse(
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        **MESSAGE,
                        "id": "msg_2",
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 7, "output_tokens": 1},
                    },
                },
            ),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 12},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
            http=httpx2,
        )
    return httpx2.Response(200, json=MESSAGE)


COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-x-2026",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
    "usage": {
        "prompt_tokens": 100,
        "completion_tokens": 9,
        "total_tokens": 109,
        "prompt_tokens_details": {"cached_tokens": 64},
    },
}
RESPONSE = {
    "id": "resp_1",
    "object": "response",
    "created_at": 1,
    "model": "gpt-x-2026",
    "status": "completed",
    "output": [],
    "parallel_tool_calls": True,
    "tool_choice": "auto",
    "tools": [],
    "usage": {
        "input_tokens": 50,
        "output_tokens": 20,
        "total_tokens": 70,
        "input_tokens_details": {"cached_tokens": 10},
        "output_tokens_details": {"reasoning_tokens": 0},
    },
}


def chunk(**fields):
    return {
        "id": "chatcmpl-2",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-x-2026",
        "choices": [],
        **fields,
    }


def openai_answer(request):
    body = json.loads(request.content)
    if request.url.path.endswith("/embeddings"):
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "embed-x",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 8, "total_tokens": 8},
            },
        )
    if request.url.path.endswith("/responses"):
        if body.get("stream"):
            return sse(
                (
                    "response.created",
                    {
                        "type": "response.created",
                        "sequence_number": 0,
                        "response": {**RESPONSE, "status": "in_progress", "usage": None},
                    },
                ),
                ("response.completed", {"type": "response.completed", "sequence_number": 1, "response": RESPONSE}),
            )
        return httpx.Response(200, json=RESPONSE)
    if body.get("stream"):
        return sse(
            (
                None,
                chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": "hel"}, "finish_reason": None}]),
            ),
            (None, chunk(choices=[{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}])),
            (None, chunk(usage={"prompt_tokens": 30, "completion_tokens": 2, "total_tokens": 32})),
            (None, "[DONE]"),
        )
    return httpx.Response(200, json=COMPLETION)


async def answer_async(handler, request):
    return handler(request)


def test_the_anthropic_clients(ingest):
    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False, transport="thread")
    sync = ai.wrap_anthropic(
        anthropic.Anthropic(api_key="k", http_client=httpx2.Client(transport=httpx2.MockTransport(anthropic_answer)))
    )
    asynchronous = ai.wrap_anthropic(
        anthropic.AsyncAnthropic(
            api_key="k",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: answer_async(anthropic_answer, r))),
        )
    )

    async def main():
        with ai.agent("async-bot"):
            msg = await asynchronous.messages.create(
                model="claude-x", max_tokens=10, messages=[{"role": "user", "content": "hi"}]
            )
            assert msg.content[0].text == "hi"
            stream = await asynchronous.messages.create(model="claude-x", max_tokens=10, stream=True, messages=[])
            async for _ in stream:
                pass

    with ai.agent("bot"):
        assert sync.messages.create(model="claude-x", max_tokens=10, messages=[]).id == "msg_1"
        with sync.messages.create(model="claude-x", max_tokens=10, stream=True, messages=[]) as stream:
            for _ in stream:
                pass
    asyncio.run(main())
    assert fixwire.flush(5)

    spans = chats(ingest)
    assert len(spans) == 4
    plain = [s for s in spans if attr(s, "gen_ai.response.id") == "msg_1"]
    streamed = [s for s in spans if attr(s, "gen_ai.response.id") == "msg_2"]
    assert len(plain) == 2 and len(streamed) == 2
    for s in plain:
        assert attr(s, "gen_ai.usage.input_tokens") == 100 and attr(s, "gen_ai.usage.cache_read.input_tokens") == 90
        assert attr(s, "gen_ai.response.model") == "claude-x-20261001"
    for s in streamed:
        assert (
            attr(s, "gen_ai.usage.output_tokens") == 12 and attr(s, "gen_ai.response.finish_reasons") == '["end_turn"]'
        )
    assert {attr(s, "gen_ai.agent.name") for s in spans} == {"bot", "async-bot"}


def test_the_openai_clients(ingest):
    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False, transport="thread")
    sync = ai.wrap_openai(
        openai.OpenAI(api_key="k", http_client=httpx.Client(transport=httpx.MockTransport(openai_answer)))
    )
    asynchronous = ai.wrap_openai(
        openai.AsyncOpenAI(
            api_key="k",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: answer_async(openai_answer, r))),
        )
    )
    assert sync.api_key == "k"

    with ai.agent("bot"):
        c = sync.chat.completions.create(
            model="gpt-x", messages=[{"role": "user", "content": "hi"}], max_completion_tokens=50
        )
        assert c.choices[0].message.content == "hi"
        text = "".join(
            ch.choices[0].delta.content or ""
            for ch in sync.chat.completions.create(
                model="gpt-x", messages=[], stream=True, stream_options={"include_usage": True}
            )
            if ch.choices
        )
        assert text == "hello"
        assert sync.responses.create(model="gpt-x", input="hi", instructions="be brief").id == "resp_1"
        sync.embeddings.create(model="embed-x", input=["a", "b"])

    async def main():
        with ai.agent("async-bot"):
            await asynchronous.chat.completions.create(model="gpt-x", messages=[])
            stream = await asynchronous.responses.create(model="gpt-x", input="hi", stream=True)
            async for _ in stream:
                pass

    asyncio.run(main())
    assert fixwire.flush(5)

    spans = chats(ingest)
    assert len(spans) == 5
    for s in spans:
        assert attr(s, "gen_ai.provider.name") == "openai" and attr(s, "fixwire.origin") == "auto.ai.openai"
    completions = [s for s in spans if attr(s, "gen_ai.response.id") == "chatcmpl-1"]
    assert len(completions) == 2
    for s in completions:
        assert attr(s, "gen_ai.usage.input_tokens") == 100 and attr(s, "gen_ai.usage.cache_read.input_tokens") == 64
        assert attr(s, "gen_ai.response.finish_reasons") == '["stop"]'
    [streamed] = [s for s in spans if attr(s, "gen_ai.response.id") == "chatcmpl-2"]
    assert attr(streamed, "gen_ai.usage.input_tokens") == 30 and attr(streamed, "gen_ai.usage.output_tokens") == 2
    responses = [s for s in spans if attr(s, "gen_ai.response.id") == "resp_1"]
    assert len(responses) == 2
    for s in responses:
        assert attr(s, "gen_ai.usage.output_tokens") == 20 and attr(s, "gen_ai.usage.cache_read.input_tokens") == 10
        assert attr(s, "gen_ai.response.finish_reasons") == '["completed"]'
    assert {attr(s, "gen_ai.agent.name") for s in spans} == {"bot", "async-bot"}
    [embed] = chats(ingest, "gen_ai.embeddings")
    assert embed["name"] == "embeddings embed-x" and attr(embed, "gen_ai.usage.input_tokens") == 8


def test_a_failed_call_is_an_error_span(ingest):
    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False)
    client = ai.wrap_openai(
        openai.OpenAI(
            api_key="k",
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}))
            ),
        )
    )
    with fixwire.start_span("job"):
        try:
            client.chat.completions.create(model="gpt-x", messages=[])
        except openai.RateLimitError:
            pass
    assert fixwire.flush(5)
    [chat] = chats(ingest)
    assert chat["status"] == "error" and attr(chat, "error.type") == "RateLimitError"
