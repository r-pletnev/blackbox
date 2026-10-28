import json
import sys
from unittest.mock import MagicMock

try:
    from ..io import load_pickle
except ImportError:
    from blackbox.io import load_pickle

BLOCKED_MODULES = [
    "config",
    "settings",
    "core.config",
    "app.config",
    "django.conf",
]


def install_import_guards() -> None:
    for module_name in BLOCKED_MODULES:
        sys.modules.setdefault(module_name, MagicMock())


def parse_import_roots(values: list[str] | None) -> list[str]:
    return [value for value in values or [] if value]


def get_locals_data(data):
    locals_data = data.get("locals", {}) if isinstance(data, dict) else {}
    if not locals_data and isinstance(data, dict) and "result" in data:
        raise RuntimeError("SNAPSHOT_HAS_NO_LOCALS")
    return locals_data


def get_result_data(data):
    result_data = data.get("result") if isinstance(data, dict) else None
    if result_data is None:
        raise RuntimeError("SNAPSHOT_HAS_NO_RESULT")
    return result_data


def serialize_psycopg2_sql(obj):
    type_name = type(obj).__name__
    if type_name == "Composed":
        return "".join(serialize_psycopg2_sql(part) for part in obj._wrapped)
    if type_name == "SQL":
        return obj._wrapped
    if type_name == "Identifier":
        if isinstance(obj._wrapped, (list, tuple)):
            return ".".join(f"`{part}`" for part in obj._wrapped)
        return f'"{obj._wrapped}"'
    if type_name == "Literal":
        value = obj._wrapped
        if value is None:
            return "NULL"
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return str(value)
    if type_name == "Placeholder":
        return f"%({obj._wrapped})s" if obj._wrapped else "%s"
    return repr(obj)


def serialize_object_view(obj, depth=0, seen=None):
    if seen is None:
        seen = set()
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if depth >= 2:
        return {"__type__": type(obj).__name__, "__repr__": repr(obj)}
    obj_id = id(obj)
    if obj_id in seen:
        return {"__type__": type(obj).__name__, "__repr__": "<recursive>"}
    seen.add(obj_id)
    if isinstance(obj, dict):
        result = {}
        for key, value in list(obj.items())[:30]:
            result[str(key)] = serialize_object_view(value, depth + 1, seen)
        return result
    if isinstance(obj, (list, tuple, set)):
        return [
            serialize_object_view(value, depth + 1, seen) for value in list(obj)[:30]
        ]

    attrs = {}
    if hasattr(obj, "__dict__") and isinstance(getattr(obj, "__dict__"), dict):
        names = list(obj.__dict__.keys())
    else:
        try:
            names = [name for name in dir(obj) if not name.startswith("_")]
        except Exception:
            names = []

    for name in names[:30]:
        try:
            value = getattr(obj, name)
            if callable(value):
                continue
            attrs[name] = serialize_object_view(value, depth + 1, seen)
        except Exception as exc:
            attrs[name] = {"__error__": str(exc)}

    return {"type": type(obj).__name__, "repr": repr(obj), "attrs": attrs}


def smart_serialize(obj):
    module = getattr(type(obj), "__module__", "") or ""
    type_name = type(obj).__name__
    if "psycopg2" in module and type_name in {
        "Composed",
        "SQL",
        "Identifier",
        "Literal",
        "Placeholder",
    }:
        try:
            return {"__type__": type_name, "__sql__": serialize_psycopg2_sql(obj)}
        except Exception as exc:
            return {"__type__": type_name, "__repr__": repr(obj), "__error__": str(exc)}
    if type_name == "DataFrame":
        try:
            return {
                "__type__": "DataFrame",
                "__shape__": list(obj.shape),
                "__columns__": list(obj.columns),
                "__head__": obj.head(5).to_dict(orient="records"),
            }
        except Exception:
            pass
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return {"__type__": type_name, "__repr__": repr(obj)}


def _json_safe_keys(value):
    """Rewrite dict keys json cannot encode (tuples, bytes, objects) as strings."""
    if isinstance(value, dict):
        return {
            key
            if key is None or isinstance(key, (str, int, float, bool))
            else repr(key): _json_safe_keys(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe_keys(item) for item in value]
    return value


def dumps_json(value) -> str:
    return json.dumps(
        _json_safe_keys(value),
        ensure_ascii=False,
        indent=2,
        default=smart_serialize,
    )
