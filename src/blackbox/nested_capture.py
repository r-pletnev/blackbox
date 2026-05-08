from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any

from .locals_capture import DEFAULT_STRATEGY
from .persistence import CallArtifact
from .session import CallContext, begin_call, finish_call


_BLACKBOX_ROOT = Path(__file__).resolve().parent
_DECORATED_CODES: set[Any] = set()


@dataclass
class _FrameFunction:
    __code__: Any
    __name__: str
    __module__: str | None


@dataclass
class _TraceEntry:
    frame: FrameType
    context: CallContext | None
    artifact: CallArtifact | None
    started_at: datetime | None
    locals: dict[str, Any]
    pending_exception: BaseException | None = None
    is_root: bool = False
    block_subtree: bool = False


def register_decorated_code(code: Any) -> None:
    _DECORATED_CODES.add(code)


def capture_nested_sync(
    func: Any,
    args: tuple,
    kwargs: dict,
    *,
    root_context: CallContext,
    save_dir: str,
    save_input: bool,
    save_output: bool,
    capture_locals: bool,
    locals_strategy: str = DEFAULT_STRATEGY,
    max_depth: int,
    serializer: str = "dill",
    sanitize_hooks: list | None = None,
    on_serialize_error: str = "warn",
    project_roots: list[str] | None = None,
) -> tuple[Any, dict | None, Exception | None]:
    original_trace = sys.gettrace()
    roots = _project_roots(func, project_roots)
    user_stack: list[_TraceEntry] = []
    entries: dict[FrameType, _TraceEntry] = {}
    root_locals: dict[str, Any] = {}

    def _trace(frame: FrameType, event: str, arg):
        if event == "call":
            return _handle_call(frame)

        entry = entries.get(frame)
        if entry is None:
            return original_trace(frame, event, arg) if original_trace else None

        if event == "line":
            entry.pending_exception = None
            if capture_locals and locals_strategy == "settrace_full":
                entry.locals.update(frame.f_locals)
            return _trace

        if event == "exception":
            exc_type, exc, _traceback = arg
            if isinstance(exc, BaseException):
                entry.pending_exception = exc
            if capture_locals:
                entry.locals.update(frame.f_locals)
            return _trace

        if event == "return":
            _finish_entry(entry, arg)
            return original_trace(frame, event, arg) if original_trace else None

        return _trace

    def _handle_call(frame: FrameType):
        if not _is_user_frame(frame, roots):
            return original_trace(frame, "call", None) if original_trace else None

        is_root = frame.f_code is func.__code__ and not user_stack
        if is_root:
            entry = _TraceEntry(
                frame=frame,
                context=None,
                artifact=None,
                started_at=None,
                locals={},
                is_root=True,
            )
            entries[frame] = entry
            user_stack.append(entry)
            return _trace

        candidate_depth = len(user_stack)
        should_block = any(item.block_subtree for item in user_stack)
        is_decorated_call = frame.f_code in _DECORATED_CODES
        should_capture = (
            not should_block
            and not is_decorated_call
            and candidate_depth < max_depth
        )

        if should_capture:
            frame_func = _frame_function(frame)
            call_args, call_kwargs = _frame_input(frame)
            context = begin_call(
                frame_func,
                call_args,
                call_kwargs,
                save_dir,
                serializer=serializer,
            )
            artifact = CallArtifact(
                frame_func,
                call_args,
                call_kwargs,
                save_dir,
                serializer=serializer,
                sanitize_hooks=sanitize_hooks,
                on_serialize_error=on_serialize_error,
                call_context=context,
            )
            if save_input:
                artifact.save_input(call_args, call_kwargs)

            entry = _TraceEntry(
                frame=frame,
                context=context,
                artifact=artifact,
                started_at=datetime.now(),
                locals={},
            )
        else:
            entry = _TraceEntry(
                frame=frame,
                context=None,
                artifact=None,
                started_at=None,
                locals={},
                block_subtree=True,
            )

        entries[frame] = entry
        user_stack.append(entry)
        return _trace

    def _finish_entry(entry: _TraceEntry, return_value: Any) -> None:
        entries.pop(entry.frame, None)
        _pop_user_stack(user_stack, entry)

        if capture_locals:
            entry.locals.update(entry.frame.f_locals)

        if entry.is_root:
            root_locals.update(entry.locals)
            return

        if entry.context is None or entry.artifact is None or entry.started_at is None:
            return

        elapsed_ms = (datetime.now() - entry.started_at).total_seconds() * 1000
        exc_info = entry.pending_exception
        result = None if exc_info is not None else return_value

        if save_output:
            entry.artifact.save_output(
                result,
                elapsed_ms,
                locals_dict=entry.locals if capture_locals else None,
                exc_info=exc_info,
            )
        finish_call(
            entry.context,
            elapsed_ms=elapsed_ms,
            exc_info=exc_info,
            locals_dict=entry.locals if capture_locals else None,
        )

    sys.settrace(_trace)
    exc_info = None
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        exc_info = exc
        result = None
    finally:
        sys.settrace(original_trace)

    return result, root_locals if capture_locals else None, exc_info


def _project_roots(func: Any, project_roots: list[str] | None) -> tuple[Path, ...]:
    if project_roots:
        roots = [Path(root).resolve() for root in project_roots]
    else:
        source_path = Path(func.__code__.co_filename).resolve()
        roots = [Path.cwd().resolve(), source_path.parent]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        value = str(root)
        if value not in seen:
            unique.append(root)
            seen.add(value)
    return tuple(unique)


def _is_user_frame(frame: FrameType, roots: tuple[Path, ...]) -> bool:
    code_name = frame.f_code.co_name
    if code_name.startswith("<"):
        return False

    raw_filename = frame.f_code.co_filename
    if raw_filename.startswith("<"):
        return False

    try:
        source_path = Path(raw_filename).resolve()
    except (OSError, RuntimeError):
        return False

    if _is_relative_to(source_path, _BLACKBOX_ROOT):
        return False
    if any(part in {".venv", "venv", "site-packages"} for part in source_path.parts):
        return False

    return any(_is_relative_to(source_path, root) for root in roots)


def _frame_function(frame: FrameType) -> _FrameFunction:
    return _FrameFunction(
        __code__=frame.f_code,
        __name__=frame.f_code.co_name,
        __module__=frame.f_globals.get("__name__"),
    )


def _frame_input(frame: FrameType) -> tuple[tuple, dict]:
    code = frame.f_code
    positional_count = code.co_argcount
    keyword_only_count = code.co_kwonlyargcount
    parameter_names = code.co_varnames[: positional_count + keyword_only_count]
    kwargs = {
        name: frame.f_locals[name]
        for name in parameter_names
        if name in frame.f_locals
    }

    index = positional_count + keyword_only_count
    if code.co_flags & inspect.CO_VARARGS and index < len(code.co_varnames):
        name = code.co_varnames[index]
        if name in frame.f_locals:
            kwargs[name] = frame.f_locals[name]
        index += 1
    if code.co_flags & inspect.CO_VARKEYWORDS and index < len(code.co_varnames):
        name = code.co_varnames[index]
        if name in frame.f_locals:
            kwargs[name] = frame.f_locals[name]

    return (), kwargs


def _pop_user_stack(stack: list[_TraceEntry], entry: _TraceEntry) -> None:
    if stack and stack[-1] is entry:
        stack.pop()
        return
    try:
        stack.remove(entry)
    except ValueError:
        pass


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
