# Exception and frame extraction ported (MIT) from upstream utils.py
# (walk_exception_chain, exceptions_from_error, single_exception_from_error_tuple,
# serialize_frame, filename_for_module and the in-app rules); copyright and
# provenance: NOTICE, UPSTREAM.md. Modified for Fixwire: local variables are captured only for in-app
# frames, through bounded reprs and a per-capture time budget; source context
# is added later, off the caller's thread (add_source_context).

from __future__ import annotations

import inspect
import linecache
import os
import re
import reprlib
import sys
import time
from collections.abc import Iterable, Iterator, Mapping
from types import FrameType, TracebackType
from typing import TYPE_CHECKING, Any, cast

from fixwire._core.jsonish import as_dict, as_list, dicts, get_dict

if TYPE_CHECKING:
    from fixwire.types import ExcInfo

_BaseExceptionGroup: type[BaseException] | None
if sys.version_info >= (3, 11):
    _BaseExceptionGroup = BaseExceptionGroup  # noqa: F821 (a builtin from 3.11)
else:  # pragma: no cover
    _BaseExceptionGroup = None

#: Time spent capturing local variables per event, after which frames are
#: sent without them.
LOCALS_BUDGET_SECONDS = 0.005
#: Local variables kept per frame.
MAX_LOCALS = 50
CONTEXT_LINES = 5

_EXTERNAL = re.compile(r"[\\/](?:dist|site)-packages[\\/]")


class Options:
    """What the builder needs from the client options."""

    __slots__ = (
        "include_local_variables",
        "max_value_length",
        "max_stack_frames",
        "in_app_include",
        "in_app_exclude",
        "project_root",
    )

    def __init__(
        self,
        include_local_variables: bool = True,
        max_value_length: int = 1024,
        max_stack_frames: int = 100,
        in_app_include: Iterable[str] = (),
        in_app_exclude: Iterable[str] = (),
        project_root: str | None = None,
    ) -> None:
        self.include_local_variables = include_local_variables
        self.max_value_length = max_value_length
        self.max_stack_frames = max_stack_frames
        self.in_app_include = list(in_app_include or ())
        self.in_app_exclude = list(in_app_exclude or ())
        self.project_root = project_root


class _Budget:
    __slots__ = ("deadline",)

    def __init__(self) -> None:
        self.deadline = time.monotonic() + LOCALS_BUDGET_SECONDS

    def left(self) -> bool:
        return time.monotonic() < self.deadline


def _bounded_repr(limit: int) -> reprlib.Repr:
    r = reprlib.Repr()
    r.maxstring = r.maxother = r.maxlong = max(16, limit)
    r.maxlevel = 3
    r.maxlist = r.maxtuple = r.maxset = r.maxfrozenset = r.maxdeque = r.maxarray = 10
    r.maxdict = 10
    return r


def safe_str(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return safe_repr(value)


def safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return "<broken repr>"


def get_type_name(cls: type | None) -> str | None:
    return getattr(cls, "__qualname__", None) or getattr(cls, "__name__", None)


def get_type_module(cls: type | None) -> str | None:
    mod = getattr(cls, "__module__", None)
    if isinstance(mod, str) and mod not in ("builtins", "__builtins__"):
        return mod
    return None


def should_hide_frame(frame: FrameType) -> bool:
    try:
        mod = frame.f_globals["__name__"]
        if mod.startswith("fixwire.") and not mod.startswith("fixwire.tests"):
            return True
    except (AttributeError, KeyError):
        pass
    for flag in ("__traceback_hide__", "__tracebackhide__"):
        try:
            if frame.f_locals[flag]:
                return True
        except Exception:
            pass
    return False


def iter_stacks(tb: TracebackType | None) -> Iterator[TracebackType]:
    while tb is not None:
        if not should_hide_frame(tb.tb_frame):
            yield tb
        tb = tb.tb_next


def filename_for_module(module: str | None, abs_path: str | None) -> str | None:
    if not abs_path or not module:
        return abs_path
    try:
        if abs_path.endswith(".pyc"):
            abs_path = abs_path[:-1]
        base_module = module.split(".", 1)[0]
        if base_module == module:
            return os.path.basename(abs_path)
        base_module_path = sys.modules[base_module].__file__
        if not base_module_path:
            return abs_path
        return abs_path.split(base_module_path.rsplit(os.sep, 2)[0], 1)[-1].lstrip(os.sep)
    except Exception:
        return abs_path


def _module_in_list(name: str | None, items: list[Any]) -> bool:
    if name is None or not items:
        return False
    return any(item == name or name.startswith(item + ".") for item in items)


def in_app(module: str | None, abs_path: str | None, o: Options) -> bool | None:
    """The usual rules: include/exclude lists, then site-packages, then the
    project root. None when nothing decides."""
    if _module_in_list(module, o.in_app_include):
        return True
    if _module_in_list(module, o.in_app_exclude):
        return False
    if abs_path is None:
        return None
    if _EXTERNAL.search(abs_path):
        return False
    if o.project_root and abs_path.startswith(o.project_root):
        return True
    return None


def serialize_frame(frame: FrameType, tb_lineno: int | None, o: Options, budget: _Budget | None) -> dict[str, Any]:
    code = getattr(frame, "f_code", None)
    abs_path = code.co_filename if code else None
    function = code.co_name if code else None
    try:
        module = frame.f_globals["__name__"]
    except Exception:
        module = None
    if tb_lineno is None:
        tb_lineno = frame.f_lineno
    try:
        os_abs_path = os.path.abspath(abs_path) if abs_path else None
    except Exception:
        os_abs_path = None
    rv: dict[str, Any] = {
        "filename": filename_for_module(module, abs_path) or None,
        "abs_path": os_abs_path,
        "function": function or "<unknown>",
        "module": module,
        "lineno": tb_lineno,
    }
    app = in_app(module, os_abs_path, o)
    if app is not None:
        rv["in_app"] = app
    if o.include_local_variables and app and budget is not None and budget.left():
        rv["vars"] = _locals(frame, o.max_value_length)
    return rv


def _locals(frame: FrameType, limit: int) -> dict[str, Any]:
    r = _bounded_repr(limit)
    out: dict[str, str] = {}
    try:
        items = list(frame.f_locals.items())
    except Exception:
        return out
    for name, value in items[:MAX_LOCALS]:
        if name.startswith("__") and name.endswith("__"):
            continue
        try:
            out[name] = r.repr(value)[:limit]
        except Exception:
            out[name] = "<broken repr>"
    return out


def get_error_message(exc_value: BaseException | None) -> str:
    message = safe_str(getattr(exc_value, "message", "") or getattr(exc_value, "detail", "") or safe_str(exc_value))
    notes = as_list(getattr(exc_value, "__notes__", None))
    if notes:
        message += "\n" + "\n".join(n for n in notes if isinstance(n, str))
    return message


def single_exception(
    exc_type: type | None,
    exc_value: BaseException | None,
    tb: TracebackType | None,
    o: Options,
    budget: _Budget,
    mechanism: Mapping[str, Any] | None = None,
    exception_id: int | None = None,
    parent_id: int | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"mechanism": dict(mechanism) if mechanism else {"type": "generic", "handled": True}}
    if exception_id is not None:
        value["mechanism"]["exception_id"] = exception_id
    errno = getattr(exc_value, "errno", None) if exc_value is not None else None
    if errno is not None:
        value["mechanism"].setdefault("meta", {}).setdefault("errno", {}).setdefault("number", errno)
    if source is not None:
        value["mechanism"]["source"] = source
    if exception_id != 0 and parent_id is not None:
        value["mechanism"]["parent_id"] = parent_id
        value["mechanism"]["type"] = "chained"
    if _BaseExceptionGroup is not None and isinstance(exc_value, _BaseExceptionGroup):
        value["mechanism"]["is_exception_group"] = True
    value["module"] = get_type_module(exc_type)
    value["type"] = get_type_name(exc_type)
    value["value"] = get_error_message(exc_value)

    tbs = list(iter_stacks(tb))
    # Keep the newest frames: they are where the error happened.
    tbs = tbs[-o.max_stack_frames :] if o.max_stack_frames else tbs
    frames = [serialize_frame(t.tb_frame, t.tb_lineno, o, budget) for t in tbs]
    if frames:
        value["stacktrace"] = {"frames": frames}
    return value


def walk_exception_chain(exc_info: ExcInfo) -> Iterator[ExcInfo]:
    exc_type, exc_value, tb = exc_info
    seen: list[BaseException] = []
    seen_ids: set[int] = set()
    while id(exc_value) not in seen_ids:
        yield exc_type, exc_value, tb
        seen.append(exc_value)
        seen_ids.add(id(exc_value))
        cause = exc_value.__cause__ if exc_value.__suppress_context__ else exc_value.__context__
        if cause is None:
            break
        exc_type, exc_value, tb = type(cause), cause, cause.__traceback__


def _exceptions_from_error(
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    tb: TracebackType | None,
    o: Options,
    budget: _Budget,
    mechanism: Mapping[str, Any] | None,
    exception_id: int,
    parent_id: int | None,
    source: str | None,
    seen_ids: set[int],
) -> tuple[int, list[dict[str, Any]]]:
    if exc_value is not None and id(exc_value) in seen_ids:
        return exception_id, []
    if exc_value is not None:
        seen_ids.add(id(exc_value))
    parent = single_exception(exc_type, exc_value, tb, o, budget, mechanism, exception_id, parent_id, source)
    out = [parent]
    parent_id, exception_id = exception_id, exception_id + 1
    nested: BaseException | None = None
    src = "__context__"
    if exc_value is not None:
        if exc_value.__suppress_context__:
            nested, src = exc_value.__cause__, "__cause__"
        else:
            nested = exc_value.__context__
    if nested is not None:
        exception_id, children = _exceptions_from_error(
            type(nested), nested, nested.__traceback__, o, budget, mechanism, exception_id, None, src, seen_ids
        )
        out.extend(children)
    group: object = getattr(exc_value, "exceptions", None)
    members = cast("tuple[object, ...]", group) if isinstance(group, (tuple, list)) else ()
    for idx, e in enumerate(members):
        if not isinstance(e, BaseException):
            continue
        exception_id, children = _exceptions_from_error(
            type(e),
            e,
            getattr(e, "__traceback__", None),
            o,
            budget,
            mechanism,
            exception_id,
            parent_id,
            "exceptions[%d]" % idx,
            seen_ids,
        )
        out.extend(children)
    return exception_id, out


def exceptions_from_error_tuple(
    exc_info: ExcInfo, o: Options, mechanism: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The event's exception values, causes first: the last one is the one
    that was raised (the order before_send sees; the wire reverses it)."""
    exc_type, exc_value, tb = exc_info
    budget = _Budget()
    values: list[dict[str, Any]]
    if _BaseExceptionGroup is not None and isinstance(exc_value, _BaseExceptionGroup):
        _, values = _exceptions_from_error(exc_type, exc_value, tb, o, budget, mechanism, 0, 0, None, set())
    else:
        values = [single_exception(t, v, b, o, budget, mechanism) for t, v, b in walk_exception_chain(exc_info)]
    values.reverse()
    return values


def exc_info_from_error(error: object) -> ExcInfo:
    """The (type, value, traceback) of an exception, of an exc_info tuple,
    or (None) of the exception being handled."""
    if error is None:
        t, v, tb = sys.exc_info()
        if t is None or v is None:
            raise ValueError("capture_exception called without an exception and outside an except block")
        return t, v, tb
    if isinstance(error, BaseException):
        tb = error.__traceback__
        if tb is None and sys.exc_info()[1] is error:
            tb = sys.exc_info()[2]
        return type(error), error, tb
    kind = type(error).__name__
    if isinstance(error, tuple) and len(cast("tuple[object, ...]", error)) == 3:
        return cast("ExcInfo", error)
    raise ValueError("expected an exception to report, got %s" % kind)


def current_stacktrace(o: Options) -> dict[str, Any]:
    """The caller's stack, for messages."""
    frames: list[dict[str, Any]] = []
    budget = _Budget()
    f: FrameType | None = inspect.currentframe()
    while f is not None:
        if not should_hide_frame(f):
            frames.append(serialize_frame(f, None, o, budget))
        f = f.f_back
    frames.reverse()
    return {"frames": frames[-o.max_stack_frames :]}


def iter_event_frames(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for st in _iter_stacktraces(event):
        yield from dicts(st.get("frames"))


def _iter_stacktraces(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    st = as_dict(event.get("stacktrace"))
    if st is not None:
        yield st
    for key in ("threads", "exception"):
        for v in dicts(get_dict(event.get(key)).get("values")):
            st = as_dict(v.get("stacktrace"))
            if st is not None:
                yield st


def add_source_context(event: dict[str, Any], max_length: int) -> None:
    """Adds lines around each frame (read from disk, cached), on the
    delivery side so the caller never waits on file I/O."""
    for frame in iter_event_frames(event):
        path, lineno = frame.get("abs_path"), frame.get("lineno")
        if not path or not isinstance(lineno, int) or "context_line" in frame:
            continue
        try:
            lines = linecache.getlines(path)
        except Exception:
            continue
        idx = lineno - 1
        if not lines or not 0 <= idx < len(lines):
            continue

        def clip(s: str) -> str:
            return s.rstrip("\r\n")[:max_length]

        frame["pre_context"] = [clip(x) for x in lines[max(0, idx - CONTEXT_LINES) : idx]]
        frame["context_line"] = clip(lines[idx])
        frame["post_context"] = [clip(x) for x in lines[idx + 1 : idx + 1 + CONTEXT_LINES]]
