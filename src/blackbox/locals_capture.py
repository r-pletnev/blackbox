# locals_capture.py
"""
Стратегии захвата локальных переменных функции.
Каждая стратегия — callable с сигнатурой:
    capture(func, args, kwargs) -> dict
"""

import sys
from collections.abc import Callable


# ── Стратегия 1: sys.settrace — финальный снимок на return ──────────────────


def capture_via_settrace(func: Callable, args: tuple, kwargs: dict) -> tuple:
    captured: dict = {}
    original_trace = sys.gettrace()

    def _local_trace(frame, event, arg):
        # ловим и return и exception — фрейм жив в обоих случаях
        if event in ("return", "exception"):
            captured.update(frame.f_locals)
        return _local_trace

    def _global_trace(frame, event, arg):
        if event == "call" and frame.f_code is func.__code__:
            return _local_trace
        return original_trace(frame, event, arg) if original_trace else None

    sys.settrace(_global_trace)
    exc_info = None
    try:
        result = func(*args, **kwargs)
    except Exception as e:
        exc_info = e  # запоминаем, не глотаем
        result = None
    finally:
        sys.settrace(original_trace)

    return result, captured, exc_info


async def capture_via_settrace_async(
    func: Callable, args: tuple, kwargs: dict
) -> tuple:
    captured: dict = {}
    original_trace = sys.gettrace()

    def _local_trace(frame, event, arg):
        if event in ("return", "exception"):
            captured.update(frame.f_locals)
        return _local_trace

    def _global_trace(frame, event, arg):
        if event == "call" and frame.f_code is func.__code__:
            return _local_trace
        return original_trace(frame, event, arg) if original_trace else None

    sys.settrace(_global_trace)
    exc_info = None
    try:
        result = await func(*args, **kwargs)
    except Exception as e:
        exc_info = e
        result = None
    finally:
        sys.settrace(original_trace)

    return result, captured, exc_info


# ── Стратегия 2: settrace — снимок на каждый line (все промежуточные) ────────


def capture_via_settrace_full(func: Callable, args: tuple, kwargs: dict) -> tuple:
    """
    Снимает f_locals на каждой строке — получаем «последнее присвоение»
    каждой переменной на протяжении всего выполнения.
    Дороже по оверхеду, зато ловит переменные из веток if, которые
    не дожили до return.
    """
    captured: dict = {}
    original_trace = sys.gettrace()

    def _local_trace(frame, event, arg):
        if event in {"line", "return"}:
            captured.update(frame.f_locals)
        return _local_trace

    def _global_trace(frame, event, arg):
        if event == "call" and frame.f_code is func.__code__:
            return _local_trace
        return original_trace(frame, event, arg) if original_trace else None

    sys.settrace(_global_trace)
    exc_info = None
    try:
        result = func(*args, **kwargs)
    except Exception as e:
        exc_info = e
        result = None
    finally:
        sys.settrace(original_trace)

    return result, captured, exc_info


async def capture_via_settrace_full_async(
    func: Callable, args: tuple, kwargs: dict
) -> tuple:
    captured: dict = {}
    original_trace = sys.gettrace()

    def _local_trace(frame, event, arg):
        if event in {"line", "return", "exception"}:
            captured.update(frame.f_locals)
        return _local_trace

    def _global_trace(frame, event, arg):
        if event == "call" and frame.f_code is func.__code__:
            return _local_trace
        return original_trace(frame, event, arg) if original_trace else None

    sys.settrace(_global_trace)
    exc_info = None
    try:
        result = await func(*args, **kwargs)
    except Exception as e:
        exc_info = e
        result = None
    finally:
        sys.settrace(original_trace)

    return result, captured, exc_info


# ── Реестр стратегий ─────────────────────────────────────────────────────────

STRATEGIES = {
    "settrace": capture_via_settrace,  # рекомендуемый
    "settrace_full": capture_via_settrace_full,  # полный снимок
}

ASYNC_STRATEGIES = {
    "settrace": capture_via_settrace_async,
    "settrace_full": capture_via_settrace_full_async,
}

DEFAULT_STRATEGY = "settrace"
