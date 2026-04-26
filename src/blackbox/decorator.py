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


def blackbox(
    save_dir: str = "/tmp/blackbox",
    *,
    save_input: bool = True,
    save_output: bool = True,
    capture_locals: bool = True,
    verbose: bool = False,
    serializer: str = "dill",
    locals_strategy: str = DEFAULT_STRATEGY,  # ← переключатель стратегий
):
    def decorator(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                artifact = CallArtifact(
                    func, args, kwargs, save_dir, serializer=serializer
                )

                if save_input:
                    artifact.save_input(args, kwargs)

                t_start = datetime.now()

                if capture_locals:
                    strategy = ASYNC_STRATEGIES[locals_strategy]
                    result, locals_dict, exc_info = await strategy(func, args, kwargs)
                else:
                    exc_info = None
                    try:
                        result = await func(*args, **kwargs)
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
                    _print_summary(func.__name__, artifact.saved)

                if exc_info is not None:
                    raise exc_info

                return result

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            artifact = CallArtifact(func, args, kwargs, save_dir, serializer=serializer)

            if save_input:
                artifact.save_input(args, kwargs)

            # ── вызов функции ────────────────────────────────────────────────
            t_start = datetime.now()

            if capture_locals:
                strategy = STRATEGIES[locals_strategy]
                result, locals_dict, exc_info = strategy(func, args, kwargs)
            else:
                exc_info = None
                try:
                    result = func(*args, **kwargs)
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
                _print_summary(func.__name__, artifact.saved)

            if exc_info is not None:
                raise exc_info

            return result

        return wrapper

    return decorator


def _print_summary(func_name: str, saved: list) -> None:
    print(f"[blackbox] {func_name}() → сохранено {len(saved)} файлов:")
    for p in saved:
        print(f"  {'📦' if p.suffix == '.pkl' else '📄'} {p}")
