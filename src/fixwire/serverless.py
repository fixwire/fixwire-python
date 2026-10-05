"""Serverless functions (AWS Lambda, Google Cloud Functions, Azure
Functions): the platform may freeze or stop the process as soon as a
handler returns, losing events still queued. ``serverless_function`` runs
each invocation in its own isolation scope, traces it as a segment named
after the function (continuing the caller's trace when the event carries
HTTP headers), reports an exception it raises (and raises it on), and
flushes before returning::

    fixwire.init(os.environ["FIXWIRE_DSN"])

    @fixwire.serverless_function
    def handler(event, context): ...

Async handlers work the same way. ``flush_timeout`` (seconds, default 2)
bounds the wait; on AWS Lambda it also stays half a second inside the time
the invocation has left.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, ParamSpec, TypeVar, cast, overload

import fixwire
from fixwire.integrations._request import session

__all__ = ["serverless_function"]

P = ParamSpec("P")
R = TypeVar("R")


def _lambda_context(args: tuple[Any, ...]) -> Any:
    """The AWS Lambda context, when the handler got one (its second argument)."""
    if len(args) > 1 and isinstance(getattr(args[1], "function_name", None), str):
        return args[1]
    return None


def _incoming(args: tuple[Any, ...]) -> dict[str, Any] | None:
    """The headers of the request that invoked the function, for its trace:
    in the event (API Gateway, function URLs, load balancers) or the request
    itself (Google Cloud Functions)."""
    if not args:
        return None
    first: object = args[0]
    headers: object
    if isinstance(first, Mapping):
        headers = cast("Mapping[str, object]", first).get("headers")
    else:
        headers = getattr(first, "headers", None)
    items: object = getattr(headers, "items", None)
    if not callable(items):
        return None
    try:
        return {str(k): v for k, v in cast("Callable[[], Iterable[tuple[object, Any]]]", items)()}
    except Exception:
        return None


def _flush_wait(timeout: float, ctx: Any) -> float:
    remaining = getattr(ctx, "get_remaining_time_in_millis", None) if ctx is not None else None
    if callable(remaining):
        try:
            left = float(cast("Callable[[], float]", remaining)()) / 1000
        except Exception:
            return timeout
        return max(0.0, min(timeout, left - 0.5))
    return timeout


def _setup(scope: fixwire.Scope, name: str, ctx: Any, args: tuple[Any, ...]) -> None:
    headers = _incoming(args)
    if headers:
        fixwire.continue_trace(headers)
    scope.set_tag("serverless.function", name)
    if ctx is not None:
        scope.set_context(
            "aws_lambda",
            {
                "function_name": getattr(ctx, "function_name", None),
                "function_version": getattr(ctx, "function_version", None),
                "aws_request_id": getattr(ctx, "aws_request_id", None),
                "invoked_function_arn": getattr(ctx, "invoked_function_arn", None),
            },
        )


def _report(error: BaseException) -> None:
    """An error escaping the function: nothing handled it."""
    client = fixwire.get_client()
    if client is not None:
        client.capture_exception(error, mechanism={"type": "serverless", "handled": False})


def _wrap(fn: Callable[P, Any], name: str | None, flush_timeout: float) -> Callable[P, Any]:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def run_async(*args: P.args, **kwargs: P.kwargs) -> Any:
            ctx = _lambda_context(args)
            label = name or (getattr(ctx, "function_name", None) if ctx is not None else None) or fn.__name__
            try:
                with fixwire.isolation_scope() as scope, session():
                    _setup(scope, label, ctx, args)
                    with fixwire.start_span(
                        label,
                        op="function.aws.lambda" if ctx is not None else "function",
                        origin="auto.function.serverless",
                    ):
                        try:
                            return await cast("Callable[P, Awaitable[Any]]", fn)(*args, **kwargs)
                        except Exception as e:
                            _report(e)
                            raise
            finally:
                wait = _flush_wait(flush_timeout, ctx)
                if wait > 0:
                    client = fixwire.get_client()
                    if isinstance(client, fixwire.AsyncClient):
                        await fixwire.aflush(wait)
                    else:
                        fixwire.flush(wait)

        return run_async

    @functools.wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> Any:
        ctx = _lambda_context(args)
        label = name or (getattr(ctx, "function_name", None) if ctx is not None else None) or fn.__name__
        try:
            with fixwire.isolation_scope() as scope, session():
                _setup(scope, label, ctx, args)
                with fixwire.start_span(
                    label,
                    op="function.aws.lambda" if ctx is not None else "function",
                    origin="auto.function.serverless",
                ):
                    try:
                        return fn(*args, **kwargs)
                    except Exception as e:
                        _report(e)
                        raise
        finally:
            wait = _flush_wait(flush_timeout, ctx)
            if wait > 0:
                fixwire.flush(wait)

    return run


@overload
def serverless_function(fn: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def serverless_function(
    *, name: str | None = None, flush_timeout: float = 2.0
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def serverless_function(
    fn: Callable[P, R] | None = None, /, *, name: str | None = None, flush_timeout: float = 2.0
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Wraps a serverless handler: its own scope, a segment, exceptions
    reported, and a flush before it returns. Use it bare or with options::

        @fixwire.serverless_function(name="charge-card", flush_timeout=1)
        async def handler(event, context): ...
    """
    if fn is not None:
        return cast("Callable[P, R]", _wrap(fn, name, flush_timeout))

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        return cast("Callable[P, R]", _wrap(f, name, flush_timeout))

    return decorate
