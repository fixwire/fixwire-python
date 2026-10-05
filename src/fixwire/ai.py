"""AI agent tracing with the OpenTelemetry GenAI conventions (``gen_ai.*``
attributes and ops), which Fixwire reads for agent runs, tool calls, tokens
and cost, and agent detectors::

    with fixwire.ai.agent("support-bot", provider="anthropic", model="claude-opus-5-5") as run:
        msg = anthropic.messages.create(...)          # traced by wrap_anthropic() (or wrap_openai())
        for block in msg.content:
            if block.type == "tool_use":
                with fixwire.ai.tool(block.name, call_id=block.id, arguments=block.input) as t:
                    t.set_result(lookup(block.input))
        run.set_output(answer)

Prompts, outputs and tool arguments are recorded only with
``record_ai_content`` (or ``record_content`` per call), bounded, and redacted
like everything else. Without it, tool arguments still get a hash, so
repeated identical calls (agent loops) are visible. The hash is the one the
JavaScript SDK computes.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterator, Mapping
from typing import (
    TYPE_CHECKING,
    Any,
    TypedDict,
    TypeVar,
    Union,
    cast,
)

import fixwire
from fixwire._core.serializer import Serializer
from fixwire._core.tracing import Span, use_span

if TYPE_CHECKING:
    from typing_extensions import Unpack

__all__ = [
    "MAX_AI_CONTENT",
    "TokenUsage",
    "ChatSpan",
    "ToolSpan",
    "AgentSpan",
    "agent",
    "chat",
    "tool",
    "embeddings",
    "wrap_anthropic",
    "wrap_openai",
    "arguments_hash",
]

#: Longest recorded content attribute (characters).
MAX_AI_CONTENT = 16_384

_serialize = Serializer(MAX_AI_CONTENT)


class TokenUsage(TypedDict, total=False):
    """Token counts of a model call."""

    input_tokens: int | None
    """All input tokens, cached ones included (as the OpenTelemetry conventions count them)."""
    output_tokens: int | None
    cache_read_input_tokens: int | None
    """Input tokens read from the provider's prompt cache."""
    cache_creation_input_tokens: int | None
    """Input tokens written to the provider's prompt cache."""


def _canonical(value: Any) -> str:
    return json.dumps(_serialize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def arguments_hash(value: Any) -> str:
    """FNV-1a 64 of the canonical JSON (sorted keys), as 16 hex digits."""
    h = 0xCBF29CE484222325
    for b in _canonical(value).encode("utf-8"):
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h


def _content(value: Any) -> str:
    s = value if isinstance(value, str) else _canonical(value)
    return s[: MAX_AI_CONTENT - 3] + "..." if len(s) > MAX_AI_CONTENT else s


def _maybe(record: bool, value: Any) -> str | None:
    return _content(value) if record and value is not None else None


def _recording(record_content: bool | None) -> bool:
    if record_content is not None:
        return record_content
    client = fixwire.get_client()
    return client is not None and client.options.record_ai_content


def _current_agent() -> str | None:
    span = fixwire.current_span()
    name = span.attributes.get("gen_ai.agent.name") if span is not None else None
    return name if isinstance(name, str) else None


def _failed(span: Span, error: BaseException) -> None:
    span.set_status("error")
    span.set_attribute("error.type", type(error).__name__)


@contextlib.contextmanager
def _run(name: str, op: str, attributes: Mapping[str, Any]) -> Generator[Span, None, None]:
    with fixwire.start_span(name, op=op, origin="manual.ai", attributes=attributes) as span:
        try:
            yield span
        except BaseException as e:
            _failed(span, e)
            raise


class ChatSpan:
    """A model call in progress: report what it returned."""

    def __init__(self, span: Span, record: bool) -> None:
        self.span = span
        self._record = record

    def set_usage(self, **usage: Unpack[TokenUsage]) -> ChatSpan:
        """Token counts (also accepted by set_response)."""
        self.span.set_attributes(
            {
                "gen_ai.usage.input_tokens": usage.get("input_tokens"),
                "gen_ai.usage.output_tokens": usage.get("output_tokens"),
                "gen_ai.usage.cache_read.input_tokens": usage.get("cache_read_input_tokens"),
                "gen_ai.usage.cache_creation.input_tokens": usage.get("cache_creation_input_tokens"),
            }
        )
        return self

    def set_response(
        self,
        *,
        id: str | None = None,
        model: str | None = None,  # noqa: A002
        finish_reasons: list[str] | None = None,
        output: Any = None,
        usage: TokenUsage | None = None,
    ) -> ChatSpan:
        """The response: id, model, finish reasons, usage, and the output (with content recording on)."""
        self.span.set_attributes(
            {
                "gen_ai.response.id": id,
                "gen_ai.response.model": model,
                "gen_ai.response.finish_reasons": json.dumps(finish_reasons) if finish_reasons is not None else None,
                "gen_ai.output.messages": _maybe(self._record, output),
            }
        )
        if usage:
            self.set_usage(**usage)
        return self


class ToolSpan:
    """A tool call in progress: report its result."""

    def __init__(self, span: Span, record: bool) -> None:
        self.span = span
        self._record = record

    def set_result(self, result: Any) -> ToolSpan:
        """The tool's result (recorded only with content recording on)."""
        self.span.set_attribute("gen_ai.tool.call.result", _maybe(self._record, result))
        return self


class AgentSpan:
    """An agent run in progress."""

    def __init__(self, span: Span, record: bool) -> None:
        self.span = span
        self._record = record

    def set_output(self, output: Any) -> AgentSpan:
        """The run's final output (recorded only with content recording on)."""
        self.span.set_attribute("gen_ai.output.messages", _maybe(self._record, output))
        return self


@contextlib.contextmanager
def agent(
    name: str,
    *,
    id: str | None = None,
    provider: str | None = None,  # noqa: A002
    model: str | None = None,
    conversation_id: str | None = None,
    input: Any = None,  # noqa: A002
    record_content: bool | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Generator[AgentSpan, None, None]:
    """An agent run: an invoke_agent span that its model and tool calls join.
    Fixwire shows it as one run, with totals, cost and detectors."""
    record = _recording(record_content)
    attrs = {
        **(attributes or {}),
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": name,
        "gen_ai.agent.id": id,
        "gen_ai.provider.name": provider,
        "gen_ai.system": provider,
        "gen_ai.request.model": model,
        "gen_ai.conversation.id": conversation_id,
        "gen_ai.input.messages": _maybe(record, input),
    }
    with _run("invoke_agent %s" % name, "gen_ai.invoke_agent", attrs) as span:
        yield AgentSpan(span, record)


def _chat_attributes(
    provider: str,
    model: str,
    record: bool,
    input: Any,
    system: Any,  # noqa: A002
    max_tokens: int | None,
    temperature: float | None,
    top_p: float | None,
    attributes: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        **(attributes or {}),
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": provider,
        "gen_ai.system": provider,
        "gen_ai.request.model": model,
        "gen_ai.request.max_tokens": max_tokens,
        "gen_ai.request.temperature": temperature,
        "gen_ai.request.top_p": top_p,
        "gen_ai.agent.name": _current_agent(),
        "gen_ai.input.messages": _maybe(record, input),
        "gen_ai.system_instructions": _maybe(record, system),
    }


@contextlib.contextmanager
def chat(
    provider: str,
    model: str,
    *,
    input: Any = None,
    system: Any = None,  # noqa: A002
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    record_content: bool | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Generator[ChatSpan, None, None]:
    """A model call: a chat span with the model, tokens and finish reasons
    (report them with ``call.set_response``). For Anthropic and OpenAI
    clients, wrap_anthropic() and wrap_openai() do this for you."""
    record = _recording(record_content)
    attrs = _chat_attributes(provider, model, record, input, system, max_tokens, temperature, top_p, attributes)
    with _run("chat %s" % model, "gen_ai.chat", attrs) as span:
        yield ChatSpan(span, record)


@contextlib.contextmanager
def tool(
    name: str,
    *,
    call_id: str | None = None,
    description: str | None = None,
    arguments: Any = None,
    record_content: bool | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Generator[ToolSpan, None, None]:
    """A tool call: an execute_tool span. An exception marks it failed with
    the error's class (the tool_error detector groups by agent, tool and class)."""
    record = _recording(record_content)
    attrs = {
        **(attributes or {}),
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        "gen_ai.tool.call.id": call_id,
        "gen_ai.tool.description": description,
        "gen_ai.agent.name": _current_agent(),
        "fixwire.tool.arguments_hash": arguments_hash(arguments) if arguments is not None else None,
        "gen_ai.tool.call.arguments": _maybe(record, arguments),
    }
    with _run("execute_tool %s" % name, "gen_ai.execute_tool", attrs) as span:
        yield ToolSpan(span, record)


@contextlib.contextmanager
def embeddings(
    provider: str,
    model: str,
    *,
    input: Any = None,  # noqa: A002
    record_content: bool | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Generator[ChatSpan, None, None]:
    """An embeddings call: report usage with ``call.set_usage``."""
    record = _recording(record_content)
    with _run(
        "embeddings %s" % model, "gen_ai.embeddings", _embeddings_attributes(provider, model, record, input, attributes)
    ) as span:
        yield ChatSpan(span, record)


def _embeddings_attributes(
    provider: str,
    model: str,
    record: bool,
    input: Any,  # noqa: A002
    attributes: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        **(attributes or {}),
        "gen_ai.operation.name": "embeddings",
        "gen_ai.provider.name": provider,
        "gen_ai.system": provider,
        "gen_ai.request.model": model,
        "gen_ai.agent.name": _current_agent(),
        "gen_ai.input.messages": _maybe(record, input),
    }


# Client wrappers for provider SDKs: each model call becomes a chat (or
# embeddings) span under the active span. The wrapped client is a proxy:
# the original is untouched, and anything not traced is the original's.
# Whether a call is async is read from what it returns: the SDKs' async
# methods are plain functions that return coroutines.


def _get(obj: Any, name: str) -> Any:
    """An attribute of an SDK object, or a key of a dict."""
    if isinstance(obj, Mapping):
        return cast("Mapping[str, Any]", obj).get(name)
    return getattr(obj, name, None)


def _list(value: Any) -> list[Any]:
    return list(cast("list[Any]", value)) if isinstance(value, (list, tuple)) else []


class _StreamReader:
    """Accumulates a streamed response's events into set_response's arguments."""

    def observe(self, ev: Any) -> None:
        raise NotImplementedError

    def response(self) -> dict[str, Any]:
        raise NotImplementedError


class _Spec:
    """How to read one SDK method's calls."""

    def __init__(
        self,
        operation: str,
        origin: str,
        attributes: Callable[[Mapping[str, Any], str, bool], dict[str, Any]],
        response: Callable[[Any], dict[str, Any]],
        reader: Callable[[], _StreamReader] | None = None,
    ) -> None:
        self.operation = operation
        self.origin = origin
        self.attributes = attributes
        self.response = response
        self.reader = reader


class _StreamState:
    """A streamed call's reader; ends the span once."""

    def __init__(self, call: ChatSpan, reader: _StreamReader) -> None:
        self.call = call
        self.reader = reader
        self.ended = False

    def observe(self, ev: Any) -> None:
        try:
            self.reader.observe(ev)
        except Exception:  # a malformed event must not break the caller's stream
            pass

    def end(self, error: BaseException | None = None) -> None:
        if self.ended:
            return
        self.ended = True
        _respond(self.call, self.reader.response)
        if error is not None:
            _failed(self.call.span, error)
        self.call.span.finish()


def _respond(call: ChatSpan, read: Callable[[], dict[str, Any]]) -> None:
    try:
        call.set_response(**read())
    except Exception:  # an unexpected response shape is not the caller's problem
        pass


class _TracedStream:
    """The SDK's stream, observed: the span ends when it's consumed, closed or fails."""

    def __init__(self, stream: Any, state: _StreamState) -> None:
        self._stream = stream
        self._state = state

    def __iter__(self) -> Iterator[Any]:
        try:
            for ev in self._stream:
                self._state.observe(ev)
                yield ev
        except BaseException as e:
            if not isinstance(e, GeneratorExit):
                self._state.end(e)
            raise
        finally:
            self._state.end()

    def __enter__(self) -> _TracedStream:
        return self

    def __exit__(self, *exc: object) -> None:
        self._state.end()
        close = getattr(self._stream, "close", None)
        if callable(close):
            close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _AsyncTracedStream:
    """The SDK's async stream, observed like _TracedStream."""

    def __init__(self, stream: Any, state: _StreamState) -> None:
        self._stream = stream
        self._state = state

    async def __aiter__(self) -> AsyncIterator[Any]:
        try:
            async for ev in self._stream:
                self._state.observe(ev)
                yield ev
        except BaseException as e:
            if not isinstance(e, GeneratorExit):
                self._state.end(e)
            raise
        finally:
            self._state.end()

    async def __aenter__(self) -> _AsyncTracedStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._state.end()
        close = getattr(self._stream, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _start(spec: _Spec, params: Mapping[str, Any]) -> ChatSpan:
    record = _recording(None)
    model = str(params.get("model") or "unknown")
    span = fixwire.start_span(
        "%s %s" % (spec.operation, model),
        op="gen_ai." + spec.operation,
        origin=spec.origin,
        attributes=spec.attributes(params, model, record),
    )
    return ChatSpan(span, record)


def _fail(call: ChatSpan, error: BaseException) -> None:
    _failed(call.span, error)
    call.span.finish()


def _done(spec: _Spec, call: ChatSpan, res: Any, params: Mapping[str, Any], is_async: bool) -> Any:
    if params.get("stream") and spec.reader is not None:
        state = _StreamState(call, spec.reader())
        return _AsyncTracedStream(res, state) if is_async else _TracedStream(res, state)
    _respond(call, lambda: spec.response(res))
    call.span.finish()
    return res


async def _done_async(spec: _Spec, call: ChatSpan, pending: Awaitable[Any], params: Mapping[str, Any]) -> Any:
    try:
        with use_span(call.span):
            res = await pending
    except BaseException as e:
        _fail(call, e)
        raise
    return _done(spec, call, res, params, True)


def _traced(spec: _Spec, method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def call(*args: Any, **params: Any) -> Any:
        chat = _start(spec, params)
        try:
            with use_span(chat.span):
                res = method(*args, **params)
        except BaseException as e:
            _fail(chat, e)
            raise
        if inspect.isawaitable(res):
            return _done_async(spec, chat, res, params)
        return _done(spec, chat, res, params, False)

    return call


_Routes = Mapping[str, Union[_Spec, "_Routes"]]


class _Proxy:
    """A client (or one of its resources) whose routed methods are traced."""

    def __init__(self, inner: Any, routes: _Routes) -> None:
        self._inner = inner
        self._routes = routes

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._inner, name)
        route = self._routes.get(name)
        if isinstance(route, _Spec):
            return _traced(route, value) if callable(value) else value
        if route is not None and value is not None:
            return _Proxy(value, route)
        return value


C = TypeVar("C")


# Anthropic.


def _anthropic_usage(u: Any) -> TokenUsage:
    """Anthropic's usage, with cache reads and writes counted in the input
    (Anthropic reports them apart from input_tokens)."""
    read, written, given = (
        _get(u, "cache_read_input_tokens"),
        _get(u, "cache_creation_input_tokens"),
        _get(u, "input_tokens"),
    )
    return {
        "input_tokens": None if given is None else given + (read or 0) + (written or 0),
        "output_tokens": _get(u, "output_tokens"),
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": written,
    }


def _anthropic_message(msg: Any) -> dict[str, Any]:
    reason = _get(msg, "stop_reason")
    return {
        "id": _get(msg, "id"),
        "model": _get(msg, "model"),
        "finish_reasons": [reason] if reason else None,
        "output": _get(msg, "content"),
        "usage": _anthropic_usage(_get(msg, "usage")),
    }


class _AnthropicStream(_StreamReader):
    def __init__(self) -> None:
        self.id: str | None = None
        self.model: str | None = None
        self.finish: list[str] | None = None
        self.usage: TokenUsage = {}
        self.texts: list[str] = []

    def observe(self, ev: Any) -> None:
        kind = _get(ev, "type")
        if kind == "message_start":
            msg = _get(ev, "message")
            self.id, self.model = _get(msg, "id"), _get(msg, "model")
            self.usage.update(_anthropic_usage(_get(msg, "usage")))
        elif kind == "message_delta":
            reason = _get(_get(ev, "delta"), "stop_reason")
            if reason:
                self.finish = [reason]
            out = _get(_get(ev, "usage"), "output_tokens")
            if out is not None:
                self.usage["output_tokens"] = out
        elif kind == "content_block_delta":
            text = _get(_get(ev, "delta"), "text")
            if isinstance(text, str):
                self.texts.append(text)

    def response(self) -> dict[str, Any]:
        output = [{"role": "assistant", "content": "".join(self.texts)}] if self.texts else None
        return {
            "id": self.id,
            "model": self.model,
            "finish_reasons": self.finish,
            "output": output,
            "usage": self.usage,
        }


_ANTHROPIC: _Routes = {
    "messages": {
        "create": _Spec(
            "chat",
            "auto.ai.anthropic",
            lambda p, model, record: _chat_attributes(
                "anthropic",
                model,
                record,
                p.get("messages"),
                p.get("system"),
                p.get("max_tokens"),
                p.get("temperature"),
                p.get("top_p"),
                None,
            ),
            _anthropic_message,
            _AnthropicStream,
        )
    }
}


def wrap_anthropic(client: C) -> C:
    """Traces an Anthropic client (``Anthropic`` or ``AsyncAnthropic``): each
    ``messages.create`` call, streaming or not, becomes a chat span with the
    model, tokens (cache tokens too) and stop reason, under the active span.
    Returns the client wrapped; the original is untouched. A streamed call's
    span ends when the stream is consumed or closed::

        anthropic = fixwire.ai.wrap_anthropic(Anthropic())
    """
    return cast(C, _Proxy(client, _ANTHROPIC))


# OpenAI.


def _openai_chat_usage(u: Any) -> TokenUsage:
    details = _get(u, "prompt_tokens_details")
    return {
        "input_tokens": _get(u, "prompt_tokens"),
        "output_tokens": _get(u, "completion_tokens"),
        "cache_read_input_tokens": _get(details, "cached_tokens"),
        "cache_creation_input_tokens": _get(details, "cache_write_tokens"),
    }


def _openai_chat(c: Any) -> dict[str, Any]:
    choices = _list(_get(c, "choices"))
    return {
        "id": _get(c, "id"),
        "model": _get(c, "model"),
        "finish_reasons": [r for r in (_get(ch, "finish_reason") for ch in choices) if r] or None,
        "output": [_get(ch, "message") for ch in choices] or None,
        "usage": _openai_chat_usage(_get(c, "usage")),
    }


class _OpenAIChatStream(_StreamReader):
    def __init__(self) -> None:
        self.id: str | None = None
        self.model: str | None = None
        self.reasons: list[str] = []
        self.texts: list[str] = []
        self.usage: Any = None

    def observe(self, ev: Any) -> None:
        self.id = self.id or _get(ev, "id")
        self.model = self.model or _get(ev, "model")
        for ch in _list(_get(ev, "choices")):
            text = _get(_get(ch, "delta"), "content")
            if isinstance(text, str):
                self.texts.append(text)
            reason = _get(ch, "finish_reason")
            if reason and reason not in self.reasons:
                self.reasons.append(reason)
        # On the last chunk with stream_options={"include_usage": True}.
        if _get(ev, "usage") is not None:
            self.usage = _get(ev, "usage")

    def response(self) -> dict[str, Any]:
        output = [{"role": "assistant", "content": "".join(self.texts)}] if self.texts else None
        return {
            "id": self.id,
            "model": self.model,
            "finish_reasons": self.reasons or None,
            "output": output,
            "usage": _openai_chat_usage(self.usage),
        }


def _openai_response(res: Any) -> dict[str, Any]:
    u = _get(res, "usage")
    details = _get(u, "input_tokens_details")
    reason = _get(_get(res, "incomplete_details"), "reason") or _get(res, "status")
    return {
        "id": _get(res, "id"),
        "model": _get(res, "model"),
        "finish_reasons": [reason] if reason else None,
        "output": _get(res, "output"),
        "usage": {
            "input_tokens": _get(u, "input_tokens"),
            "output_tokens": _get(u, "output_tokens"),
            "cache_read_input_tokens": _get(details, "cached_tokens"),
            "cache_creation_input_tokens": _get(details, "cache_write_tokens"),
        },
    }


class _OpenAIResponsesStream(_StreamReader):
    def __init__(self) -> None:
        self.last: Any = None

    def observe(self, ev: Any) -> None:
        # response.created … response.completed / .failed / .incomplete carry the response.
        kind = _get(ev, "type")
        if isinstance(kind, str) and kind.startswith("response.") and _get(ev, "response") is not None:
            self.last = _get(ev, "response")

    def response(self) -> dict[str, Any]:
        return _openai_response(self.last) if self.last is not None else {}


def _openai_embeddings(e: Any) -> dict[str, Any]:
    return {"model": _get(e, "model"), "usage": {"input_tokens": _get(_get(e, "usage"), "prompt_tokens")}}


_OPENAI: _Routes = {
    "chat": {
        "completions": {
            "create": _Spec(
                "chat",
                "auto.ai.openai",
                lambda p, model, record: _chat_attributes(
                    "openai",
                    model,
                    record,
                    p.get("messages"),
                    None,
                    p.get("max_completion_tokens") or p.get("max_tokens"),
                    p.get("temperature"),
                    p.get("top_p"),
                    None,
                ),
                _openai_chat,
                _OpenAIChatStream,
            )
        }
    },
    "responses": {
        "create": _Spec(
            "chat",
            "auto.ai.openai",
            lambda p, model, record: _chat_attributes(
                "openai",
                model,
                record,
                p.get("input"),
                p.get("instructions"),
                p.get("max_output_tokens"),
                p.get("temperature"),
                p.get("top_p"),
                None,
            ),
            _openai_response,
            _OpenAIResponsesStream,
        )
    },
    "embeddings": {
        "create": _Spec(
            "embeddings",
            "auto.ai.openai",
            lambda p, model, record: _embeddings_attributes("openai", model, record, p.get("input"), None),
            _openai_embeddings,
        )
    },
}


def wrap_openai(client: C) -> C:
    """Traces an OpenAI client (``OpenAI`` or ``AsyncOpenAI``, or an
    OpenAI-compatible one): each ``chat.completions.create``,
    ``responses.create`` and ``embeddings.create`` call, streaming or not,
    becomes a span with the model, tokens (cached ones too) and finish
    reasons, under the active span. For token counts on streamed chat
    completions, ask for them with ``stream_options={"include_usage": True}``.
    Returns the client wrapped; the original is untouched::

        openai = fixwire.ai.wrap_openai(OpenAI())
    """
    return cast(C, _Proxy(client, _OPENAI))
