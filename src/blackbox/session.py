from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .io import _get_git_branch, _make_hash

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


SESSION_ID_ENV = "BLACKBOX_SESSION_ID"
SESSION_DIR_ENV = "BLACKBOX_SESSION_DIR"
SCHEMA_VERSION = 1

_CALL_STACK: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "blackbox_call_stack",
    default=(),
)
_THREAD_LOCK = threading.RLock()


@dataclass(frozen=True)
class CallContext:
    session_id: str
    session_dir: Path
    call_id: str
    sequence: int
    parent_call_id: str | None
    depth: int
    timestamp: str
    call_hash: str
    artifact_dir: Path
    artifact_dir_rel: str
    started_at: str
    pid: int
    thread_id: int
    token: contextvars.Token[tuple[str, ...]]


def begin_call(
    func: Any,
    args: tuple,
    kwargs: dict,
    save_dir: str,
    *,
    serializer: str = "dill",
) -> CallContext:
    session_id, session_dir = _get_or_create_session(save_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    call_hash = _make_hash(args, kwargs, serializer=serializer)
    stack = _CALL_STACK.get()
    parent_call_id = stack[-1] if stack else None
    depth = len(stack)
    pid = os.getpid()
    thread_id = threading.get_ident()
    started_at = _now_iso()

    with _session_lock(session_dir):
        meta = _load_or_create_meta(session_id, session_dir)
        sequence = _next_sequence(session_dir)
        call_id = f"c{sequence:06d}"
        artifact_dir_rel = _artifact_dir_rel(func, sequence, timestamp, call_hash)
        artifact_dir = session_dir / artifact_dir_rel
        meta["calls"].append(
            {
                "id": call_id,
                "sequence": sequence,
                "parentCallId": parent_call_id,
                "depth": depth,
                "status": "running",
                "function": getattr(func, "__name__", repr(func)),
                "module": getattr(func, "__module__", None),
                "sourcePath": str(Path(func.__code__.co_filename).resolve()),
                "line": getattr(func.__code__, "co_firstlineno", None),
                "isAsync": _is_async(func),
                "pid": pid,
                "threadId": thread_id,
                "startedAt": started_at,
                "endedAt": None,
                "elapsedMs": None,
                "exception": None,
                "callHash": call_hash,
                "artifactDir": artifact_dir_rel,
            }
        )
        meta["updatedAt"] = started_at
        _save_json_atomic(meta, session_dir / "meta.json")

    token = _CALL_STACK.set((*stack, call_id))
    return CallContext(
        session_id=session_id,
        session_dir=session_dir,
        call_id=call_id,
        sequence=sequence,
        parent_call_id=parent_call_id,
        depth=depth,
        timestamp=timestamp,
        call_hash=call_hash,
        artifact_dir=artifact_dir,
        artifact_dir_rel=artifact_dir_rel,
        started_at=started_at,
        pid=pid,
        thread_id=thread_id,
        token=token,
    )


def finish_call(
    context: CallContext,
    *,
    elapsed_ms: float | None = None,
    exc_info: BaseException | None = None,
    locals_dict: dict | None = None,
) -> None:
    ended_at = _now_iso()
    status = "failed" if exc_info is not None else "completed"
    exception_type = type(exc_info).__name__ if exc_info is not None else None

    try:
        with _session_lock(context.session_dir):
            meta = _load_or_create_meta(context.session_id, context.session_dir)
            for call in meta["calls"]:
                if call.get("id") == context.call_id:
                    call["status"] = status
                    call["endedAt"] = ended_at
                    call["elapsedMs"] = (
                        round(elapsed_ms, 3) if elapsed_ms is not None else None
                    )
                    call["exception"] = exception_type
                    if locals_dict:
                        call["locals"] = sorted(locals_dict)
                    break
            meta["updatedAt"] = ended_at
            _save_json_atomic(meta, context.session_dir / "meta.json")
    finally:
        _CALL_STACK.reset(context.token)


def _get_or_create_session(save_dir: str) -> tuple[str, Path]:
    with _THREAD_LOCK:
        env_session_id = os.environ.get(SESSION_ID_ENV)
        env_session_dir = os.environ.get(SESSION_DIR_ENV)
        if env_session_id and env_session_dir:
            session_id = env_session_id
            session_dir = Path(env_session_dir)
        else:
            session_id = _new_session_id()
            session_dir = Path(save_dir).resolve() / "sessions" / session_id
            os.environ[SESSION_ID_ENV] = session_id
            os.environ[SESSION_DIR_ENV] = str(session_dir)

        session_dir.mkdir(parents=True, exist_ok=True)
        with _session_lock(session_dir):
            _load_or_create_meta(session_id, session_dir)
        return session_id, session_dir


def _load_or_create_meta(session_id: str, session_dir: Path) -> dict[str, Any]:
    meta_path = session_dir / "meta.json"
    if meta_path.exists():
        with meta_path.open(encoding="utf-8") as file:
            return json.load(file)

    created_at = _now_iso()
    meta = {
        "schemaVersion": SCHEMA_VERSION,
        "sessionId": session_id,
        "createdAt": created_at,
        "updatedAt": created_at,
        "cwd": str(Path.cwd()),
        "gitBranch": _get_git_branch(),
        "calls": [],
    }
    _save_json_atomic(meta, meta_path)
    return meta


def _next_sequence(session_dir: Path) -> int:
    counter_path = session_dir / "counter.txt"
    try:
        current = int(counter_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        current = 0
    value = current + 1
    counter_path.write_text(str(value), encoding="utf-8")
    return value


@contextmanager
def _session_lock(session_dir: Path):
    session_dir.mkdir(parents=True, exist_ok=True)
    lock_path = session_dir / "lock"
    with _THREAD_LOCK:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _save_json_atomic(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _artifact_dir_rel(
    func: Any,
    sequence: int,
    timestamp: str,
    call_hash: str,
) -> str:
    source_path = Path(func.__code__.co_filename).resolve()
    source_component = _source_component(source_path)
    function_component = _sanitize_component(getattr(func, "__name__", "unknown"))
    call_component = f"{sequence:06d}_{timestamp}_{call_hash}"
    return Path(
        "artifacts",
        source_component,
        function_component,
        call_component,
    ).as_posix()


def _source_component(source_path: Path) -> str:
    digest = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:6]
    if source_path.suffix:
        value = f"{source_path.stem}_{digest}{source_path.suffix}"
    else:
        value = f"{source_path.name}_{digest}"
    return _sanitize_component(value)


def _sanitize_component(value: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in value
    )
    return cleaned.strip("._") or "unknown"


def _new_session_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{timestamp}_{os.getpid()}_{uuid.uuid4().hex[:6]}"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _is_async(func: Any) -> bool:
    import inspect

    return inspect.iscoroutinefunction(func)
