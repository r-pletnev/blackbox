# persistence.py
"""
Логика сохранения: pickle + meta.json.
Ничего не знает о декораторе и стратегиях захвата.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from .io import _get_git_branch, _make_hash, _sanitize_for_pickle, _save_meta_json, _save_pickle


class CallArtifact:
    """
    Всё что связано с одним вызовом функции.
    Создаётся один раз, передаётся между этапами сохранения.
    """

    def __init__(
        self,
        func,
        args: tuple,
        kwargs: dict,
        save_dir: str,
        *,
        serializer: str = "dill",
    ):
        base_dir = Path(save_dir)
        source_file = Path(func.__code__.co_filename).name  # ← фикс: берём из func, не из frame
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        call_hash = _make_hash(args, kwargs, serializer=serializer)

        self.call_dir = base_dir / source_file / func.__name__ / f"{timestamp}_{call_hash}"
        self.serializer = serializer
        self.meta = {
            "function": func.__name__,
            "module": func.__module__,
            "timestamp": timestamp,
            "call_hash": call_hash,
            "serializer": serializer,
        }
        self.saved: list[Path] = []

    # ── input ────────────────────────────────────────────────────────────────

    def save_input(self, args: tuple, kwargs: dict) -> None:
        data = {"args": args, "kwargs": kwargs}
        self._persist(data, "input")

    # ── output ───────────────────────────────────────────────────────────────

    def save_output(
        self,
        result: Any,
        elapsed_ms: float,
        locals_dict: dict | None = None,
        exc_info: Exception | None = None,
    ) -> None:
        data: dict = {
            "result": _sanitize_for_pickle(result, serializer=self.serializer),
            "duration_ms": round(elapsed_ms, 3),
            "exception": None,
        }

        exception_type = None

        if exc_info is not None:
            import traceback

            exception_type = type(exc_info).__name__
            data["exception"] = {
                "type": exception_type,
                "message": str(exc_info),
                "traceback": "".join(
                    traceback.format_exception(type(exc_info), exc_info, exc_info.__traceback__)
                ),
            }

        if locals_dict is not None:
            data["locals"] = locals_dict

        self._persist(
            data,
            "output",
            extra_meta={"elapsed_ms": round(elapsed_ms, 3)},
            exception_type=exception_type,
            locals_dict=locals_dict,
        )

    # ── internal ─────────────────────────────────────────────────────────────

    def _persist(
        self,
        data: dict,
        name: str,
        *,
        extra_meta: dict | None = None,
        exception_type: str | None = None,
        locals_dict: dict | None = None,
    ) -> None:
        pkl_path = self.call_dir / f"{name}.pkl"
        pkl_path.parent.mkdir(parents=True, exist_ok=True)

        sanitized = _sanitize_for_pickle(data, serializer=self.serializer)
        if _save_pickle(sanitized, str(pkl_path), serializer=self.serializer):
            self.saved.append(pkl_path)

        git_branch = _get_git_branch()
        meta_data = {
            "function": self.meta["function"],
            "module": self.meta["module"],
            "callHash": self.meta["call_hash"],
            "gitBranch": git_branch,
            "created": self.meta["timestamp"],
            "role": name,
            "serializer": self.serializer,
            "exception": exception_type,
            "locals": sorted(locals_dict.keys()) if locals_dict else [],
        }
        if extra_meta and extra_meta.get("elapsed_ms"):
            meta_data["elapsedMs"] = extra_meta["elapsed_ms"]

        meta_path = self.call_dir / "meta.json"
        _save_meta_json(meta_data, str(meta_path))
        self.saved.append(meta_path)
