import hashlib
import importlib
import json
import pickle
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _get_serializer_module(serializer: str):
    """Return serializer module by name."""
    if serializer == "pickle":
        return pickle
    if serializer == "dill":
        try:
            return importlib.import_module("dill")
        except ImportError as exc:
            raise RuntimeError(
                "Serializer 'dill' requires dependency 'dill'. "
                "Reinstall or update blackbox so its package dependencies are installed."
            ) from exc
    raise ValueError(f"Unsupported serializer: {serializer!r}")


def _is_picklable(obj: Any, *, serializer: str = "dill") -> bool:
    """Check if object can be serialized by the selected backend."""
    serializer_module = _get_serializer_module(serializer)
    try:
        serializer_module.dumps(obj)
    except Exception:
        return False
    else:
        return True


def _sanitize_for_pickle(
    obj: Any,
    _seen: set[int] | None = None,
    *,
    serializer: str = "dill",
) -> Any:
    """Replace unserializable objects with serializable representations."""
    import io

    if _seen is None:
        _seen = set()

    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return obj

    obj_id = id(obj)
    if obj_id in _seen:
        return {"__recursive_ref__": type(obj).__name__}

    if isinstance(obj, io.TextIOWrapper):
        return {
            "__file_handle__": True,
            "name": obj.name,
            "mode": obj.mode,
            "encoding": obj.encoding,
        }

    if _is_picklable(obj, serializer=serializer):
        return obj

    _seen.add(obj_id)
    try:
        if isinstance(obj, Mapping):
            return {
                k: _sanitize_for_pickle(v, _seen, serializer=serializer)
                for k, v in obj.items()
            }

        if isinstance(obj, tuple):
            return tuple(_sanitize_for_pickle(item, _seen, serializer=serializer) for item in obj)

        if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
            return type(obj)(
                _sanitize_for_pickle(item, _seen, serializer=serializer) for item in obj
            )

        if isinstance(obj, set):
            return {_sanitize_for_pickle(item, _seen, serializer=serializer) for item in obj}

        if isinstance(obj, frozenset):
            return frozenset(
                _sanitize_for_pickle(item, _seen, serializer=serializer) for item in obj
            )

        if hasattr(obj, "__dict__"):
            return {
                "__object__": type(obj).__name__,
                **{
                    k: _sanitize_for_pickle(v, _seen, serializer=serializer)
                    for k, v in obj.__dict__.items()
                },
            }

        return {"__unpicklable__": type(obj).__name__, "__repr__": repr(obj)}
    finally:
        _seen.discard(obj_id)


def _save_pickle(data: Any, path: str, *, serializer: str = "dill") -> bool:
    """Save data to binary file. Returns False if not serializable."""
    serializer_module = _get_serializer_module(serializer)
    if not _is_picklable(data, serializer=serializer):
        print(
            f"[blackbox] ⚠️  Skipping {serializer}: {type(data).__name__} "
            f"is not serializable"
        )
        return False
    with Path.open(Path(path), "wb") as f:
        serializer_module.dump(data, f)
    return True


def _save_meta_json(data: dict[str, Any], path: str) -> None:
    """Сохраняет служебный meta.json с простыми сериализуемыми значениями."""
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def load_pickle(path: str, *, serializer: str = "auto") -> Any:
    """Загружает бинарный артефакт через выбранный backend."""
    if serializer == "auto":
        try:
            return load_pickle(path, serializer="dill")
        except (pickle.UnpicklingError, EOFError, AttributeError, ImportError, ModuleNotFoundError, ValueError, TypeError):
            return load_pickle(path, serializer="pickle")

    serializer_module = _get_serializer_module(serializer)
    with Path(path).open("rb") as f:
        return serializer_module.load(f)  # noqa: S301


def load_json(path: str) -> Any:
    """Загружает JSON-файл, например meta.json."""
    with Path(path).open(encoding="utf-8") as f:
        return json.load(f)


def _make_hash(args, kwargs: Any, *, serializer: str = "dill") -> str:
    try:
        raw = repr(
            _sanitize_for_pickle(
                {"args": args, "kwargs": kwargs},
                serializer=serializer,
            )
        )
    except Exception:
        raw = str(args) + str(kwargs)
    return hashlib.md5(raw.encode()).hexdigest()[:8]


def _get_git_branch() -> str | None:
    """Get current git branch, returns None if not in a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path.cwd(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None
