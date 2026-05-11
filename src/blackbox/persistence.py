# persistence.py
"""
Логика сохранения: pickle + meta.json.
Ничего не знает о декораторе и стратегиях захвата.
"""

import inspect
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .io import (
    _fallback_unserializable_marker,
    _get_git_branch,
    _issue_record,
    _make_hash,
    _report_issue,
    _sanitize_for_pickle,
    _save_meta_json,
    _save_pickle,
    _to_plain_data,
    _try_serialize,
)
from .session import CallContext


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
        sanitize_hooks: list | None = None,
        on_serialize_error: str = "warn",
        call_context: CallContext | None = None,
    ):
        base_dir = Path(save_dir)
        source_path = Path(func.__code__.co_filename).resolve()
        source_file = source_path.name
        timestamp = (
            call_context.timestamp
            if call_context is not None
            else datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        )
        call_hash = (
            call_context.call_hash
            if call_context is not None
            else _make_hash(args, kwargs, serializer=serializer)
        )

        self.call_context = call_context
        self.call_dir = (
            call_context.artifact_dir
            if call_context is not None
            else base_dir / source_file / func.__name__ / f"{timestamp}_{call_hash}"
        )
        self.serializer = serializer
        self.sanitize_hooks = sanitize_hooks or []
        self.on_serialize_error = on_serialize_error
        try:
            function_source = inspect.getsource(func)
        except (OSError, TypeError):
            function_source = None
        self.func = func
        self.meta = {
            "function": func.__name__,
            "module": func.__module__,
            "timestamp": timestamp,
            "call_hash": call_hash,
            "serializer": serializer,
            "source_path": str(source_path),
            "is_async": inspect.iscoroutinefunction(func),
            "function_source": function_source,
            "cwd": str(Path.cwd()),
            "sys_path": self._capture_sys_path(),
        }
        if call_context is not None:
            self.meta.update(
                {
                    "session_id": call_context.session_id,
                    "call_id": call_context.call_id,
                    "sequence": call_context.sequence,
                    "parent_call_id": call_context.parent_call_id,
                    "depth": call_context.depth,
                    "artifact_dir": call_context.artifact_dir_rel,
                    "pid": call_context.pid,
                    "thread_id": call_context.thread_id,
                }
            )
        self.saved: list[Path] = []

    @staticmethod
    def _capture_sys_path() -> list[str]:
        entries: list[str] = []
        seen: set[str] = set()
        for raw in sys.path:
            if not raw:
                raw = str(Path.cwd())
            try:
                value = str(Path(raw).resolve())
            except Exception:
                continue
            if value not in seen:
                entries.append(value)
                seen.add(value)
        return entries

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
        memory: dict[str, Any] | None = None,
    ) -> None:
        blackbox_meta: dict[str, Any] = {
            "function_source": self.meta["function_source"],
        }
        function_issues: list[dict[str, Any]] = []
        function_module = getattr(self.func, "__module__", None)
        if function_module == "__main__":
            function_ok = False
            function_reason = (
                "function object belongs to __main__ and is not portable across loading contexts"
            )
        else:
            function_ok, function_reason = _try_serialize(
                self.func,
                serializer=self.serializer,
            )

        if function_ok:
            blackbox_meta["function"] = self.func
        else:
            issue = _issue_record(
                "output['__blackbox__']['function']",
                self.func,
                reason=function_reason or "function object is not serializable",
                action="omitted function object",
            )
            _report_issue(
                issue,
                issues=function_issues,
                on_serialize_error=self.on_serialize_error,
            )

        data: dict = {
            "result": result,
            "duration_ms": round(elapsed_ms, 3),
            "exception": None,
            "__blackbox__": blackbox_meta,
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
            extra_meta={"elapsed_ms": round(elapsed_ms, 3), "memory": memory},
            exception_type=exception_type,
            locals_dict=locals_dict,
            precomputed_issues=function_issues,
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
        precomputed_issues: list[dict[str, Any]] | None = None,
    ) -> None:
        pkl_path = self.call_dir / f"{name}.pkl"
        pkl_path.parent.mkdir(parents=True, exist_ok=True)

        issues: list[dict[str, Any]] = list(precomputed_issues or [])
        sanitized = _sanitize_for_pickle(
            data,
            serializer=self.serializer,
            sanitize_hooks=self.sanitize_hooks,
            on_serialize_error=self.on_serialize_error,
            issues=issues,
            path=name,
        )
        if name == "output" and isinstance(sanitized, dict):
            blackbox_meta = sanitized.setdefault("__blackbox__", {})
            if isinstance(blackbox_meta, dict):
                blackbox_meta["serialization_issues"] = issues
        saved = _save_pickle(sanitized, str(pkl_path), serializer=self.serializer)
        if not saved and name == "output" and isinstance(sanitized, dict):
            retried = dict(sanitized)
            blackbox_meta = retried.get("__blackbox__", {})
            if isinstance(blackbox_meta, dict) and "function" in blackbox_meta:
                issue = _issue_record(
                    "output['__blackbox__']['function']",
                    self.func,
                    reason="removed during final retry because the full output payload was still not serializable",
                    action="removed function object and retried output.pkl save",
                )
                issues.append(issue)
                blackbox_meta = dict(blackbox_meta)
                blackbox_meta.pop("function", None)
                blackbox_meta["serialization_issues"] = issues
                retried["__blackbox__"] = blackbox_meta
                saved = _save_pickle(retried, str(pkl_path), serializer=self.serializer)
                if saved:
                    sanitized = retried

        if not saved and name == "output":
            degraded_issue = {
                "path": "output",
                "type": type(sanitized).__name__,
                "module": type(sanitized).__module__,
                "reason": "output payload remained unserializable after final retry",
                "action": "stored plain-data degraded output payload",
            }
            degraded = _to_plain_data(sanitized, path=name)
            if not isinstance(degraded, dict):
                degraded = {
                    "result": _fallback_unserializable_marker(
                        sanitized.get("result") if isinstance(sanitized, dict) else sanitized,
                        "output payload remained unserializable after final retry",
                    ),
                    "duration_ms": sanitized.get("duration_ms") if isinstance(sanitized, dict) else None,
                    "exception": sanitized.get("exception") if isinstance(sanitized, dict) else None,
                    "locals": {},
                    "__blackbox__": {
                        "function_source": self.meta["function_source"],
                    },
                }

            blackbox_meta = degraded.setdefault("__blackbox__", {})
            if isinstance(blackbox_meta, dict):
                blackbox_meta.setdefault("function_source", self.meta["function_source"])
                blackbox_meta["serialization_issues"] = issues + [degraded_issue]
            saved = _save_pickle(degraded, str(pkl_path), serializer=self.serializer)

        if saved:
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
            "sourcePath": self.meta["source_path"],
            "cwd": self.meta["cwd"],
            "sysPath": self.meta["sys_path"],
            "isAsync": self.meta["is_async"],
            "title": self.call_dir.name,
            "exception": exception_type,
            "locals": sorted(locals_dict.keys()) if locals_dict else [],
        }
        if self.call_context is not None:
            meta_data.update(
                {
                    "sessionId": self.meta["session_id"],
                    "callId": self.meta["call_id"],
                    "sequence": self.meta["sequence"],
                    "parentCallId": self.meta["parent_call_id"],
                    "depth": self.meta["depth"],
                    "artifactDir": self.meta["artifact_dir"],
                    "pid": self.meta["pid"],
                    "threadId": self.meta["thread_id"],
                }
            )
        if extra_meta and extra_meta.get("elapsed_ms"):
            meta_data["elapsedMs"] = extra_meta["elapsed_ms"]
        if extra_meta and extra_meta.get("memory") is not None:
            meta_data["memory"] = extra_meta["memory"]

        meta_path = self.call_dir / "meta.json"
        _save_meta_json(meta_data, str(meta_path))
        self.saved.append(meta_path)
