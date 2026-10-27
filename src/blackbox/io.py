import hashlib
import importlib
import inspect
import io
import json
import pickle
import re
import subprocess
import sys
import warnings
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

NOT_HANDLED = object()
_MISSING_MAIN_ATTR_RE = re.compile(r"Can't get attribute '([^']+)' on <module '__main__'")
_MISSING_FILE_RE = re.compile(r"\[Errno 2\] No such file or directory: '([^']+)'")
_MP_CONNECTION_DEL_GUARD_INSTALLED = False


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


def _parse_missing_file_path(exc: Exception) -> str | None:
    match = _MISSING_FILE_RE.search(str(exc))
    if match:
        return match.group(1)
    return None


def _parse_missing_module_name(exc: Exception) -> str | None:
    if isinstance(exc, ModuleNotFoundError):
        return getattr(exc, "name", None)
    return None


def _load_neighbor_meta(path: str) -> dict[str, Any] | None:
    meta_path = Path(path).with_name("meta.json")
    if not meta_path.exists():
        return None
    try:
        with meta_path.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _snapshot_import_roots(path: str) -> list[str]:
    meta = _load_neighbor_meta(path)
    if not meta:
        return []

    roots: list[str] = []
    seen: set[str] = set()

    sys_path_entries = meta.get("sysPath")
    if isinstance(sys_path_entries, list):
        for entry in sys_path_entries:
            if not isinstance(entry, str) or not entry:
                continue
            try:
                value = str(Path(entry).resolve())
            except Exception:
                value = entry
            if value not in seen:
                roots.append(value)
                seen.add(value)

    cwd_value = meta.get("cwd")
    if isinstance(cwd_value, str) and cwd_value:
        try:
            value = str(Path(cwd_value).resolve())
        except Exception:
            value = cwd_value
        if value not in seen:
            roots.insert(0, value)
            seen.add(value)

    source_path_raw = meta.get("sourcePath")
    if not isinstance(source_path_raw, str) or not source_path_raw:
        return roots

    source_path = Path(source_path_raw).resolve()
    module_name = meta.get("module")
    candidates: list[Path] = []

    if source_path.parent.exists():
        candidates.append(source_path.parent)

    current = source_path.parent
    for _ in range(6):
        if current.exists():
            candidates.append(current)
        if current.parent == current:
            break
        current = current.parent

    if isinstance(module_name, str) and module_name:
        parts = module_name.split(".")
        current = source_path.parent
        ascend = len(parts) if source_path.name == "__init__.py" else max(len(parts) - 1, 0)
        for _ in range(ascend):
            if current.parent == current:
                break
            current = current.parent
        if current.exists():
            candidates.insert(0, current)

    for candidate in candidates:
        value = str(candidate)
        if value not in seen:
            roots.append(value)
            seen.add(value)
    return roots


@contextmanager
def _temporary_sys_path(entries: list[str]):
    added = [entry for entry in entries if entry and entry not in sys.path]
    for entry in reversed(added):
        sys.path.insert(0, entry)
    try:
        yield
    finally:
        for entry in added:
            try:
                sys.path.remove(entry)
            except ValueError:
                pass


def _merge_import_roots(*groups: list[str] | tuple[str, ...] | None) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if not group:
            continue
        for entry in group:
            if not entry:
                continue
            try:
                value = str(Path(entry).resolve())
            except Exception:
                value = str(entry)
            if value not in seen:
                merged.append(value)
                seen.add(value)
    return merged


def _load_dill_file_with_options(
    serializer_module,
    path: str,
    *,
    ignore: bool | None = None,
    allow_missing_filehandles: bool = False,
) -> Any:
    kwargs = {} if ignore is None else {"ignore": ignore}
    if not allow_missing_filehandles:
        with Path(path).open("rb") as f:
            return serializer_module.load(f, **kwargs)

    dill_impl = getattr(serializer_module, "_dill", None)
    if dill_impl is None or not hasattr(dill_impl, "_create_filehandle"):
        with Path(path).open("rb") as f:
            return serializer_module.load(f, **kwargs)

    original_create_filehandle = dill_impl._create_filehandle

    def _patched_create_filehandle(
        name,
        mode,
        position,
        closed,
        open,
        strictio,
        fmode,
        fdata,
    ):
        try:
            return original_create_filehandle(
                name,
                mode,
                position,
                closed,
                open,
                strictio,
                fmode,
                fdata,
            )
        except pickle.UnpicklingError as exc:
            if _parse_missing_file_path(exc) is None:
                raise

            if "b" in mode:
                handle = io.BytesIO(fdata if isinstance(fdata, (bytes, bytearray)) else b"")
            else:
                text = fdata if isinstance(fdata, str) else ""
                handle = io.StringIO(text)

            try:
                handle.name = name
            except Exception:
                pass

            if position:
                handle.seek(position)
            if closed:
                handle.close()
            return handle

    dill_impl._create_filehandle = _patched_create_filehandle
    try:
        with Path(path).open("rb") as f:
            return serializer_module.load(f, **kwargs)
    finally:
        dill_impl._create_filehandle = original_create_filehandle


def _install_safe_multiprocessing_connection_guard() -> None:
    global _MP_CONNECTION_DEL_GUARD_INSTALLED
    if _MP_CONNECTION_DEL_GUARD_INSTALLED:
        return

    try:
        import multiprocessing.connection as mp_connection
    except ImportError:
        return

    if not hasattr(mp_connection, "_ConnectionBase"):
        return

    original_connection_del = mp_connection._ConnectionBase.__del__

    def _patched_connection_del(self):
        try:
            original_connection_del(self)
        except OSError as exc:
            if exc.errno != 9:
                raise

    mp_connection._ConnectionBase.__del__ = _patched_connection_del
    _MP_CONNECTION_DEL_GUARD_INSTALLED = True


def _load_dill_with_fallbacks(path: str, *, import_roots: list[str] | None = None) -> Any:
    serializer_module = _get_serializer_module("dill")
    import_roots = _merge_import_roots(import_roots, _snapshot_import_roots(path))

    try:
        with _temporary_sys_path(import_roots):
            return _load_dill_file_with_options(serializer_module, path)
    except AttributeError as exc:
        missing_name = _parse_missing_main_attribute(exc)
        if missing_name is None:
            if _parse_missing_file_path(exc) is None:
                raise
    except pickle.UnpicklingError as exc:
        if _parse_missing_file_path(exc) is None:
            raise
    except ModuleNotFoundError as exc:
        if _parse_missing_module_name(exc) is None:
            raise

    try:
        with _temporary_sys_path(import_roots):
            return _load_dill_file_with_options(
                serializer_module,
                path,
                ignore=True,
                allow_missing_filehandles=True,
            )
    except TypeError:
        try:
            with _temporary_sys_path(import_roots):
                return _load_dill_file_with_options(
                    serializer_module,
                    path,
                    allow_missing_filehandles=True,
                )
        except pickle.UnpicklingError as exc:
            if _parse_missing_file_path(exc) is None:
                raise
        except ModuleNotFoundError as exc:
            if _parse_missing_module_name(exc) is None:
                raise
    except AttributeError as exc:
        missing_name = _parse_missing_main_attribute(exc)
        if missing_name is None:
            if _parse_missing_file_path(exc) is None:
                raise
    except pickle.UnpicklingError as exc:
        if _parse_missing_file_path(exc) is None:
            raise
    except ModuleNotFoundError as exc:
        if _parse_missing_module_name(exc) is None:
            raise

    main_module = sys.modules.get("__main__")
    if main_module is None:
        raise RuntimeError("Unable to resolve __main__ module for dill fallback loading.")

    injected_names: list[str] = []
    last_exc: Exception | None = None
    while True:
        try:
            with _temporary_sys_path(import_roots):
                return _load_dill_file_with_options(
                    serializer_module,
                    path,
                    ignore=True,
                    allow_missing_filehandles=True,
                )
        except TypeError:
            try:
                with _temporary_sys_path(import_roots):
                    return _load_dill_file_with_options(
                        serializer_module,
                        path,
                        allow_missing_filehandles=True,
                    )
            except AttributeError as exc:
                last_exc = exc
                missing_name = _parse_missing_main_attribute(exc)
                if missing_name is None:
                    raise
            except pickle.UnpicklingError:
                raise
            except ModuleNotFoundError as exc:
                missing_name = _parse_missing_module_name(exc)
                if missing_name is None:
                    raise
                last_exc = exc
            except Exception:
                raise
        except AttributeError as exc:
            last_exc = exc
            missing_name = _parse_missing_main_attribute(exc)
            if missing_name is None:
                raise
        except ModuleNotFoundError as exc:
            missing_name = _parse_missing_module_name(exc)
            if missing_name is None:
                raise
            last_exc = exc

        if isinstance(last_exc, ModuleNotFoundError):
            raise last_exc

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


def _neutralize_runtime_bound_objects(
    obj: Any,
    _seen: set[int] | None = None,
) -> Any:
    if _seen is None:
        _seen = set()

    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return obj

    obj_id = id(obj)
    if obj_id in _seen:
        return obj
    _seen.add(obj_id)

    try:
        try:
            import multiprocessing.connection as mp_connection
        except ImportError:
            mp_connection = None

        if mp_connection is not None and isinstance(obj, mp_connection._ConnectionBase):
            try:
                obj._handle = None
            except Exception:
                pass
            try:
                obj._close = lambda *args, **kwargs: None
            except Exception:
                pass
            return obj

        if isinstance(obj, Mapping):
            for key, value in list(_mapping_items(obj)):
                try:
                    _neutralize_runtime_bound_objects(key, _seen)
                    _neutralize_runtime_bound_objects(value, _seen)
                except Exception:
                    continue
            return obj

        if isinstance(obj, (list, tuple, set, frozenset)):
            for item in obj:
                _neutralize_runtime_bound_objects(item, _seen)
            return obj

        if hasattr(obj, "__dict__") and isinstance(getattr(obj, "__dict__", None), dict):
            for value in obj.__dict__.values():
                _neutralize_runtime_bound_objects(value, _seen)
            return obj

        return obj
    finally:
        _seen.discard(obj_id)


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
    type_name = type(obj).__name__
    module_name = type(obj).__module__
    return {
        "__blackbox_unserializable__": True,
        "type": type_name,
        "module": module_name,
        "repr": f"<{module_name}.{type_name}>",
        "reason": reason,
    }


def _object_module_name(obj: Any) -> str | None:
    if inspect.isfunction(obj) or inspect.ismethod(obj) or inspect.isclass(obj):
        return getattr(obj, "__module__", None)
    return getattr(type(obj), "__module__", None)


def _must_not_probe_for_serialization(obj: Any) -> bool:
    """Return whether pickling this live resource may affect its owner process."""
    module_name = _object_module_name(obj) or ""
    return module_name == "psycopg2.extensions" and type(obj).__name__ in {
        "connection",
        "cursor",
    }


def _mapping_items(obj: Mapping) -> Any:
    """Iterate a mapping without letting one broken item abort a snapshot."""
    try:
        iterator = iter(obj.items())
    except Exception as exc:
        yield _mapping_error_key(exc), None
        return

    while True:
        try:
            item = next(iterator)
        except StopIteration:
            return
        except Exception as exc:
            yield _mapping_error_key(exc), None
            return

        try:
            key, value = item
        except Exception as exc:
            yield _mapping_error_key(exc), None
            continue
        yield key, value


def _mapping_error_key(exc: Exception, *, key: Any = NOT_HANDLED) -> str:
    try:
        message = str(exc)
    except Exception:
        message = "<unprintable exception>"

    if key is NOT_HANDLED:
        item = ""
    else:
        try:
            item = f" while processing {key!r}"
        except Exception:
            item = f" while processing <{type(key).__name__}>"
    return f"__blackbox_mapping_error__{item}: {type(exc).__name__}: {message}"


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
    if _must_not_probe_for_serialization(obj):
        return True

    obj_id = id(obj)
    if obj_id in _seen:
        return False

    _seen.add(obj_id)
    try:
        if isinstance(obj, Mapping):
            for key, value in _mapping_items(obj):
                if isinstance(key, str) and key.startswith("__blackbox_mapping_error__:"):
                    return True
                try:
                    contains_runtime_locals = _contains_runtime_local_objects(
                        key, _seen
                    ) or _contains_runtime_local_objects(value, _seen)
                except Exception:
                    return True
                if contains_runtime_locals:
                    return True
            return False

        if isinstance(obj, (tuple, list, set, frozenset)):
            return any(_contains_runtime_local_objects(item, _seen) for item in obj)

        attributes = getattr(obj, "__dict__", None)
        if isinstance(attributes, dict):
            return any(
                _must_not_probe_for_serialization(value)
                for value in attributes.values()
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
            for key, value in _mapping_items(obj):
                try:
                    if isinstance(key, (str, int, float, bool)) or key is None:
                        plain_key = str(key)
                    else:
                        plain_key = repr(
                            _to_plain_data(key, _seen, path=f"{path}[key]")
                        )
                    plain[plain_key] = _to_plain_data(
                        value, _seen, path=f"{path}[{plain_key!r}]"
                    )
                except Exception as exc:
                    plain[_mapping_error_key(exc, key=key)] = None
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

    if _must_not_probe_for_serialization(obj):
        reason = "live database connection or cursor is excluded from serialization"
        issue = _issue_record(
            path,
            obj,
            reason=reason,
            action="stored fallback marker without probing the resource",
        )
        _report_issue(
            issue,
            issues=issues,
            on_serialize_error=on_serialize_error,
        )
        return _fallback_unserializable_marker(obj, reason)

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
            "contains runtime-local objects or live resources that are not portable "
            "across loading contexts"
        )
    else:
        serializable, serialize_reason = _try_serialize(obj, serializer=serializer)
    if serializable:
        return obj

    _seen.add(obj_id)
    try:
        if isinstance(obj, Mapping):
            sanitized: dict[Any, Any] = {}
            for key, value in _mapping_items(obj):
                try:
                    sanitized_key = _normalize_mapping_key(
                        key,
                        _seen,
                        serializer=serializer,
                        sanitize_hooks=sanitize_hooks,
                        on_serialize_error=on_serialize_error,
                        issues=issues,
                        path=f"{path}[key:{key!r}]",
                    )
                    sanitized[sanitized_key] = _sanitize_for_pickle(
                        value,
                        _seen,
                        serializer=serializer,
                        sanitize_hooks=sanitize_hooks,
                        on_serialize_error=on_serialize_error,
                        issues=issues,
                        path=f"{path}[{key!r}]",
                    )
                except Exception as exc:
                    sanitized[_mapping_error_key(exc, key=key)] = None
            return sanitized

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


def load_pickle(
    path: str,
    *,
    serializer: str = "auto",
    import_roots: Sequence[str] | None = None,
) -> Any:
    """Загружает бинарный артефакт через выбранный backend."""
    if serializer == "auto":
        try:
            return load_pickle(path, serializer="dill", import_roots=import_roots)
        except (pickle.UnpicklingError, EOFError, AttributeError, ImportError, ModuleNotFoundError, ValueError, TypeError):
            return load_pickle(path, serializer="pickle", import_roots=import_roots)

    if serializer == "dill":
        _install_safe_multiprocessing_connection_guard()
        return _neutralize_runtime_bound_objects(
            _load_dill_with_fallbacks(path, import_roots=list(import_roots or []))
        )

    serializer_module = _get_serializer_module(serializer)
    with Path(path).open("rb") as f:
        return serializer_module.load(f)


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
