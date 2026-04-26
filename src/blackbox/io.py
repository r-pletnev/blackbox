import hashlib
import importlib
import inspect
import json
import pickle
import re
import subprocess
import sys
import warnings
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

NOT_HANDLED = object()
_MISSING_MAIN_ATTR_RE = re.compile(r"Can't get attribute '([^']+)' on <module '__main__'")


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


class _MissingRuntimeType:
    """Placeholder used to recover old dill snapshots with __main__ references."""

    def __init__(self, *args, **kwargs):
        self.__blackbox_missing_args__ = args
        self.__blackbox_missing_kwargs__ = kwargs

    def __setstate__(self, state):
        if isinstance(state, dict):
            self.__dict__.update(state)
        else:
            self.__blackbox_state__ = state

    def __repr__(self) -> str:
        return f"<MissingRuntimeType {type(self).__name__}>"


def _missing_runtime_type(name: str):
    return type(name, (_MissingRuntimeType,), {"__module__": "__main__"})


def _parse_missing_main_attribute(exc: Exception) -> str | None:
    match = _MISSING_MAIN_ATTR_RE.search(str(exc))
    if match:
        return match.group(1)
    return None


def _load_dill_with_fallbacks(path: str) -> Any:
    serializer_module = _get_serializer_module("dill")

    try:
        with Path(path).open("rb") as f:
            return serializer_module.load(f)  # noqa: S301
    except AttributeError as exc:
        missing_name = _parse_missing_main_attribute(exc)
        if missing_name is None:
            raise

    try:
        with Path(path).open("rb") as f:
            return serializer_module.load(f, ignore=True)  # noqa: S301
    except TypeError:
        pass
    except AttributeError as exc:
        missing_name = _parse_missing_main_attribute(exc)
        if missing_name is None:
            raise

    main_module = sys.modules.get("__main__")
    if main_module is None:
        raise RuntimeError("Unable to resolve __main__ module for dill fallback loading.")

    injected_names: list[str] = []
    last_exc: Exception | None = None
    while True:
        try:
            with Path(path).open("rb") as f:
                return serializer_module.load(f, ignore=True)  # noqa: S301
        except TypeError:
            try:
                with Path(path).open("rb") as f:
                    return serializer_module.load(f)  # noqa: S301
            except AttributeError as exc:
                last_exc = exc
                missing_name = _parse_missing_main_attribute(exc)
                if missing_name is None:
                    raise
            except Exception:
                raise
        except AttributeError as exc:
            last_exc = exc
            missing_name = _parse_missing_main_attribute(exc)
            if missing_name is None:
                raise

        if hasattr(main_module, missing_name):
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("dill fallback loading failed without a recoverable missing attribute.")

        setattr(main_module, missing_name, _missing_runtime_type(missing_name))
        injected_names.append(missing_name)
        if len(injected_names) >= 32:
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("Exceeded dill fallback placeholder limit while loading snapshot.")


def _try_serialize(obj: Any, *, serializer: str = "dill") -> tuple[bool, str | None]:
    """Try to serialize an object and return a structured failure reason."""
    serializer_module = _get_serializer_module(serializer)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            serializer_module.dumps(obj)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    else:
        return True, None


def _is_picklable(obj: Any, *, serializer: str = "dill") -> bool:
    """Check if object can be serialized by the selected backend."""
    ok, _ = _try_serialize(obj, serializer=serializer)
    return ok


def _issue_record(path: str, obj: Any, reason: str, action: str) -> dict[str, Any]:
    return {
        "path": path,
        "type": type(obj).__name__,
        "module": type(obj).__module__,
        "reason": reason,
        "action": action,
    }


def _report_issue(
    issue: dict[str, Any],
    *,
    issues: list[dict[str, Any]] | None,
    on_serialize_error: str,
) -> None:
    if issues is not None:
        issues.append(issue)

    if on_serialize_error == "silent":
        return
    if on_serialize_error == "warn":
        print(
            "[blackbox] ⚠️  "
            f"Failed to serialize {issue['path']} "
            f"(type={issue['type']}, module={issue['module']}): "
            f"{issue['reason']}; {issue['action']}."
        )
        return
    if on_serialize_error == "raise":
        raise RuntimeError(
            f"Failed to serialize {issue['path']} "
            f"(type={issue['type']}, module={issue['module']}): {issue['reason']}"
        )
    raise ValueError(f"Unsupported on_serialize_error mode: {on_serialize_error!r}")


def _apply_sanitize_hooks(
    obj: Any,
    *,
    path: str,
    serializer: str,
    sanitize_hooks: Sequence | None,
    on_serialize_error: str,
    issues: list[dict[str, Any]] | None,
) -> Any:
    if not sanitize_hooks:
        return NOT_HANDLED

    context = {"path": path, "serializer": serializer}
    for hook in sanitize_hooks:
        try:
            sanitized = hook(obj, context)
        except Exception as exc:
            issue = _issue_record(
                path,
                obj,
                reason=f"sanitize hook {getattr(hook, '__name__', repr(hook))} failed: {type(exc).__name__}: {exc}",
                action="skipped hook and continued",
            )
            _report_issue(
                issue,
                issues=issues,
                on_serialize_error=on_serialize_error,
            )
            continue

        if sanitized is NOT_HANDLED:
            continue
        return sanitized

    return NOT_HANDLED


def _fallback_unserializable_marker(obj: Any, reason: str) -> dict[str, Any]:
    return {
        "__blackbox_unserializable__": True,
        "type": type(obj).__name__,
        "module": type(obj).__module__,
        "repr": repr(obj),
        "reason": reason,
    }


def _object_module_name(obj: Any) -> str | None:
    if inspect.isfunction(obj) or inspect.ismethod(obj) or inspect.isclass(obj):
        return getattr(obj, "__module__", None)
    return getattr(type(obj), "__module__", None)


def _contains_runtime_local_objects(
    obj: Any,
    _seen: set[int] | None = None,
) -> bool:
    if _seen is None:
        _seen = set()

    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return False

    module_name = _object_module_name(obj)
    if module_name == "__main__":
        return True

    obj_id = id(obj)
    if obj_id in _seen:
        return False

    _seen.add(obj_id)
    try:
        if isinstance(obj, Mapping):
            return any(
                _contains_runtime_local_objects(key, _seen)
                or _contains_runtime_local_objects(value, _seen)
                for key, value in obj.items()
            )

        if isinstance(obj, (tuple, list, set, frozenset)):
            return any(_contains_runtime_local_objects(item, _seen) for item in obj)

        if hasattr(obj, "__dict__") and isinstance(obj.__dict__, dict):
            return any(
                _contains_runtime_local_objects(value, _seen)
                for value in obj.__dict__.values()
            )

        return False
    finally:
        _seen.discard(obj_id)


def _normalize_mapping_key(
    key: Any,
    _seen: set[int],
    *,
    serializer: str,
    sanitize_hooks: Sequence | None,
    on_serialize_error: str,
    issues: list[dict[str, Any]] | None,
    path: str,
) -> Any:
    if key is None or isinstance(key, (bool, int, float, str, bytes)):
        return key

    sanitized_key = _sanitize_for_pickle(
        key,
        _seen,
        serializer=serializer,
        sanitize_hooks=sanitize_hooks,
        on_serialize_error=on_serialize_error,
        issues=issues,
        path=path,
    )
    try:
        hash(sanitized_key)
    except TypeError:
        return repr(sanitized_key)
    return sanitized_key


def _to_plain_data(
    obj: Any,
    _seen: set[int] | None = None,
    *,
    path: str = "root",
) -> Any:
    """Convert an arbitrary object graph to plain Python builtins."""
    import enum
    import io

    if _seen is None:
        _seen = set()

    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return obj

    obj_id = id(obj)
    if obj_id in _seen:
        return {"__recursive_ref__": type(obj).__name__}

    _seen.add(obj_id)
    try:
        if isinstance(obj, io.TextIOWrapper):
            return {
                "__file_handle__": True,
                "name": obj.name,
                "mode": obj.mode,
                "encoding": obj.encoding,
            }

        if isinstance(obj, enum.Enum):
            return {
                "__enum__": True,
                "class": type(obj).__name__,
                "module": type(obj).__module__,
                "name": obj.name,
                "value": _to_plain_data(obj.value, _seen, path=f"{path}.value"),
            }

        if isinstance(obj, enum.EnumMeta):
            return {
                "__enum_class__": True,
                "class": obj.__name__,
                "module": obj.__module__,
                "members": list(obj.__members__.keys()),
            }

        if isinstance(obj, Mapping):
            plain: dict[str, Any] = {}
            for key, value in obj.items():
                if isinstance(key, (str, int, float, bool)) or key is None:
                    plain_key = str(key)
                else:
                    plain_key = repr(_to_plain_data(key, _seen, path=f"{path}[key]"))
                plain[plain_key] = _to_plain_data(value, _seen, path=f"{path}[{plain_key!r}]")
            return plain

        if isinstance(obj, tuple):
            return [
                _to_plain_data(item, _seen, path=f"{path}[{index}]")
                for index, item in enumerate(obj)
            ]

        if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
            return [
                _to_plain_data(item, _seen, path=f"{path}[{index}]")
                for index, item in enumerate(obj)
            ]

        if isinstance(obj, (set, frozenset)):
            return [
                _to_plain_data(item, _seen, path=f"{path}[{index}]")
                for index, item in enumerate(obj)
            ]

        if hasattr(obj, "__dict__"):
            return {
                "__object__": type(obj).__name__,
                **{
                    str(key): _to_plain_data(value, _seen, path=f"{path}.{key}")
                    for key, value in obj.__dict__.items()
                },
            }

        return _fallback_unserializable_marker(
            obj,
            "converted to plain-data fallback because the sanitized payload remained unserializable",
        )
    finally:
        _seen.discard(obj_id)


def _sanitize_for_pickle(
    obj: Any,
    _seen: set[int] | None = None,
    *,
    serializer: str = "dill",
    sanitize_hooks: Sequence | None = None,
    on_serialize_error: str = "warn",
    issues: list[dict[str, Any]] | None = None,
    path: str = "root",
) -> Any:
    """Replace unserializable objects with serializable representations."""
    import enum
    import io

    if _seen is None:
        _seen = set()

    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return obj

    obj_id = id(obj)
    if obj_id in _seen:
        return {"__recursive_ref__": type(obj).__name__}

    hooked = _apply_sanitize_hooks(
        obj,
        path=path,
        serializer=serializer,
        sanitize_hooks=sanitize_hooks,
        on_serialize_error=on_serialize_error,
        issues=issues,
    )
    if hooked is not NOT_HANDLED:
        return _sanitize_for_pickle(
            hooked,
            _seen,
            serializer=serializer,
            sanitize_hooks=sanitize_hooks,
            on_serialize_error=on_serialize_error,
            issues=issues,
            path=path,
        )

    if isinstance(obj, io.TextIOWrapper):
        return {
            "__file_handle__": True,
            "name": obj.name,
            "mode": obj.mode,
            "encoding": obj.encoding,
        }

    if isinstance(obj, enum.Enum):
        return {
            "__enum__": True,
            "class": type(obj).__name__,
            "module": type(obj).__module__,
            "name": obj.name,
            "value": _sanitize_for_pickle(
                obj.value,
                _seen,
                serializer=serializer,
                sanitize_hooks=sanitize_hooks,
                on_serialize_error=on_serialize_error,
                issues=issues,
                path=f"{path}.value",
            ),
        }

    if isinstance(obj, enum.EnumMeta):
        return {
            "__enum_class__": True,
            "class": obj.__name__,
            "module": obj.__module__,
            "members": list(obj.__members__.keys()),
        }

    if _contains_runtime_local_objects(obj):
        serializable = False
        serialize_reason = (
            "contains objects from __main__, which are not portable across loading contexts"
        )
    else:
        serializable, serialize_reason = _try_serialize(obj, serializer=serializer)
    if serializable:
        return obj

    _seen.add(obj_id)
    try:
        if isinstance(obj, Mapping):
            return {
                _normalize_mapping_key(
                    k,
                    _seen,
                    serializer=serializer,
                    sanitize_hooks=sanitize_hooks,
                    on_serialize_error=on_serialize_error,
                    issues=issues,
                    path=f"{path}[key:{k!r}]",
                ): _sanitize_for_pickle(
                    v,
                    _seen,
                    serializer=serializer,
                    sanitize_hooks=sanitize_hooks,
                    on_serialize_error=on_serialize_error,
                    issues=issues,
                    path=f"{path}[{k!r}]",
                )
                for k, v in obj.items()
            }

        if isinstance(obj, tuple):
            return tuple(
                _sanitize_for_pickle(
                    item,
                    _seen,
                    serializer=serializer,
                    sanitize_hooks=sanitize_hooks,
                    on_serialize_error=on_serialize_error,
                    issues=issues,
                    path=f"{path}[{index}]",
                )
                for index, item in enumerate(obj)
            )

        if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
            return type(obj)(
                _sanitize_for_pickle(
                    item,
                    _seen,
                    serializer=serializer,
                    sanitize_hooks=sanitize_hooks,
                    on_serialize_error=on_serialize_error,
                    issues=issues,
                    path=f"{path}[{index}]",
                )
                for index, item in enumerate(obj)
            )

        if isinstance(obj, set):
            return [
                _sanitize_for_pickle(
                    item,
                    _seen,
                    serializer=serializer,
                    sanitize_hooks=sanitize_hooks,
                    on_serialize_error=on_serialize_error,
                    issues=issues,
                    path=f"{path}[{index}]",
                )
                for index, item in enumerate(obj)
            ]

        if isinstance(obj, frozenset):
            return [
                _sanitize_for_pickle(
                    item,
                    _seen,
                    serializer=serializer,
                    sanitize_hooks=sanitize_hooks,
                    on_serialize_error=on_serialize_error,
                    issues=issues,
                    path=f"{path}[{index}]",
                )
                for index, item in enumerate(obj)
            ]

        if hasattr(obj, "__dict__"):
            return {
                "__object__": type(obj).__name__,
                **{
                    k: _sanitize_for_pickle(
                        v,
                        _seen,
                        serializer=serializer,
                        sanitize_hooks=sanitize_hooks,
                        on_serialize_error=on_serialize_error,
                        issues=issues,
                        path=f"{path}.{k}",
                    )
                    for k, v in obj.__dict__.items()
                },
            }

        reason = serialize_reason or "object is not serializable"
        issue = _issue_record(
            path,
            obj,
            reason=reason,
            action="stored fallback marker",
        )
        _report_issue(
            issue,
            issues=issues,
            on_serialize_error=on_serialize_error,
        )
        return _fallback_unserializable_marker(obj, reason)
    finally:
        _seen.discard(obj_id)


def _save_pickle(data: Any, path: str, *, serializer: str = "dill") -> bool:
    """Save data to binary file. Returns False if not serializable."""
    serializer_module = _get_serializer_module(serializer)
    serializable, reason = _try_serialize(data, serializer=serializer)
    if not serializable:
        print(
            f"[blackbox] ⚠️  Skipping {serializer}: {type(data).__name__} "
            f"is not serializable ({reason})"
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

    if serializer == "dill":
        return _load_dill_with_fallbacks(path)

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
                on_serialize_error="silent",
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
