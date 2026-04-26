# decorator.py
"""
Декоратор-оркестратор — только координация, никакой логики сохранения/захвата.
"""

import functools
import inspect
from collections.abc import Callable
from datetime import datetime

from .locals_capture import ASYNC_STRATEGIES, DEFAULT_STRATEGY, STRATEGIES
from .persistence import CallArtifact


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
):
    def decorator(func: Callable) -> Callable:
        target_func, descriptor_type = _unwrap_descriptor(func)

        if inspect.iscoroutinefunction(target_func):

            @functools.wraps(target_func)
            async def async_wrapper(*args, **kwargs):
                artifact = CallArtifact(
                    target_func,
                    args,
                    kwargs,
                    save_dir,
                    serializer=serializer,
                    sanitize_hooks=sanitize_hooks,
                    on_serialize_error=on_serialize_error,
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
                    exc_info = None
                    try:
                        result = await target_func(*args, **kwargs)
                    except Exception as e:
                        exc_info = e
                        result = None
                    locals_dict = None

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

            return _rewrap_descriptor(async_wrapper, descriptor_type)

        @functools.wraps(target_func)
        def wrapper(*args, **kwargs):
            artifact = CallArtifact(
                target_func,
                args,
                kwargs,
                save_dir,
                serializer=serializer,
                sanitize_hooks=sanitize_hooks,
                on_serialize_error=on_serialize_error,
            )

            if save_input:
                artifact.save_input(args, kwargs)

            # ── вызов функции ────────────────────────────────────────────────
            t_start = datetime.now()

            if capture_locals:
                strategy = STRATEGIES[locals_strategy]
                result, locals_dict, exc_info = strategy(target_func, args, kwargs)
            else:
                exc_info = None
                try:
                    result = target_func(*args, **kwargs)
                except Exception as e:
                    exc_info = e
                    result = None
                locals_dict = None

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

        return _rewrap_descriptor(wrapper, descriptor_type)

    return decorator


def _print_summary(func_name: str, saved: list) -> None:
    print(f"[blackbox] {func_name}() → сохранено {len(saved)} файлов:")
    for p in saved:
        print(f"  {'📦' if p.suffix == '.pkl' else '📄'} {p}")
