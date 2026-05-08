# decorator.py
"""
Декоратор-оркестратор — только координация, никакой логики сохранения/захвата.
"""

import functools
import inspect
import sys
import warnings
from collections.abc import Callable
from datetime import datetime

from .locals_capture import ASYNC_STRATEGIES, DEFAULT_STRATEGY, STRATEGIES
from .nested_capture import capture_nested_sync, register_decorated_code
from .persistence import CallArtifact
from .session import begin_call, finish_call


def _unwrap_descriptor(func: Callable) -> tuple[Callable, type | None]:
    if isinstance(func, classmethod):
        return func.__func__, classmethod
    if isinstance(func, staticmethod):
        return func.__func__, staticmethod
    return func, None


def _rewrap_descriptor(func: Callable, descriptor_type: type | None) -> Callable:
    if descriptor_type is None:
        return func
    return descriptor_type(func)


def blackbox(
    save_dir: str = "/tmp/blackbox",
    *,
    save_input: bool = True,
    save_output: bool = True,
    capture_locals: bool = True,
    verbose: bool = False,
    serializer: str = "dill",
    sanitize_hooks: list | None = None,
    on_serialize_error: str = "warn",
    locals_strategy: str = DEFAULT_STRATEGY,  # ← переключатель стратегий
    max_depth: int = 1,
    project_roots: list[str] | None = None,
):
    if max_depth < 1:
        raise ValueError("max_depth must be greater than or equal to 1")

    def decorator(func: Callable) -> Callable:
        target_func, descriptor_type = _unwrap_descriptor(func)
        register_decorated_code(target_func.__code__)

        if inspect.iscoroutinefunction(target_func):

            @functools.wraps(target_func)
            async def async_wrapper(*args, **kwargs):
                if max_depth > 1:
                    warnings.warn(
                        "blackbox max_depth > 1 is not supported for async "
                        "functions yet; falling back to max_depth=1",
                        RuntimeWarning,
                        stacklevel=2,
                    )

                call_context = begin_call(
                    target_func,
                    args,
                    kwargs,
                    save_dir,
                    serializer=serializer,
                )
                elapsed_ms = None
                exc_info = None
                locals_dict = None
                try:
                    artifact = CallArtifact(
                        target_func,
                        args,
                        kwargs,
                        save_dir,
                        serializer=serializer,
                        sanitize_hooks=sanitize_hooks,
                        on_serialize_error=on_serialize_error,
                        call_context=call_context,
                    )

                    if save_input:
                        artifact.save_input(args, kwargs)

                    t_start = datetime.now()

                    if capture_locals:
                        strategy = ASYNC_STRATEGIES[locals_strategy]
                        result, locals_dict, exc_info = await strategy(
                            target_func, args, kwargs
                        )
                    else:
                        try:
                            result = await target_func(*args, **kwargs)
                        except Exception as e:
                            exc_info = e
                            result = None

                    elapsed_ms = (datetime.now() - t_start).total_seconds() * 1000

                    if save_output:
                        artifact.save_output(
                            result,
                            elapsed_ms,
                            locals_dict=locals_dict,
                            exc_info=exc_info,
                        )

                    if verbose:
                        _print_summary(target_func.__name__, artifact.saved)

                    if exc_info is not None:
                        raise exc_info

                    return result
                finally:
                    active_exc = sys.exc_info()[1]
                    finish_call(
                        call_context,
                        elapsed_ms=elapsed_ms,
                        exc_info=exc_info or active_exc,
                        locals_dict=locals_dict,
                    )

            return _rewrap_descriptor(async_wrapper, descriptor_type)

        @functools.wraps(target_func)
        def wrapper(*args, **kwargs):
            call_context = begin_call(
                target_func,
                args,
                kwargs,
                save_dir,
                serializer=serializer,
            )
            elapsed_ms = None
            exc_info = None
            locals_dict = None
            try:
                artifact = CallArtifact(
                    target_func,
                    args,
                    kwargs,
                    save_dir,
                    serializer=serializer,
                    sanitize_hooks=sanitize_hooks,
                    on_serialize_error=on_serialize_error,
                    call_context=call_context,
                )

                if save_input:
                    artifact.save_input(args, kwargs)

                # ── вызов функции ────────────────────────────────────────────────
                t_start = datetime.now()

                if max_depth > 1:
                    result, locals_dict, exc_info = capture_nested_sync(
                        target_func,
                        args,
                        kwargs,
                        root_context=call_context,
                        save_dir=save_dir,
                        save_input=save_input,
                        save_output=save_output,
                        capture_locals=capture_locals,
                        locals_strategy=locals_strategy,
                        max_depth=max_depth,
                        serializer=serializer,
                        sanitize_hooks=sanitize_hooks,
                        on_serialize_error=on_serialize_error,
                        project_roots=project_roots,
                    )
                elif capture_locals:
                    strategy = STRATEGIES[locals_strategy]
                    result, locals_dict, exc_info = strategy(target_func, args, kwargs)
                else:
                    try:
                        result = target_func(*args, **kwargs)
                    except Exception as e:
                        exc_info = e
                        result = None

                elapsed_ms = (datetime.now() - t_start).total_seconds() * 1000
                # ────────────────────────────────────────────────────────────────

                if save_output:
                    artifact.save_output(
                        result,
                        elapsed_ms,
                        locals_dict=locals_dict,
                        exc_info=exc_info,
                    )

                if verbose:
                    _print_summary(target_func.__name__, artifact.saved)

                if exc_info is not None:
                    raise exc_info

                return result
            finally:
                active_exc = sys.exc_info()[1]
                finish_call(
                    call_context,
                    elapsed_ms=elapsed_ms,
                    exc_info=exc_info or active_exc,
                    locals_dict=locals_dict,
                )

        return _rewrap_descriptor(wrapper, descriptor_type)

    return decorator


def _print_summary(func_name: str, saved: list) -> None:
    print(f"[blackbox] {func_name}() → сохранено {len(saved)} файлов:")
    for p in saved:
        print(f"  {'📦' if p.suffix == '.pkl' else '📄'} {p}")
