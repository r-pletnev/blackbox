import types
from pathlib import Path

import pytest

from blackbox import blackbox
from blackbox.io import (
    _MAX_INTROSPECTION_DEPTH,
    _contains_runtime_local_objects,
    _fallback_unserializable_marker,
    _neutralize_runtime_bound_objects,
    _sanitize_for_pickle,
    _to_plain_data,
    load_pickle,
)

SESSION_ENV_VARS = ("BLACKBOX_SESSION_ID", "BLACKBOX_SESSION_DIR")


@pytest.fixture(autouse=True)
def clean_blackbox_session_env(monkeypatch):
    for name in SESSION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class connection:
    """Stand-in for psycopg2.extensions.connection."""

    __module__ = "psycopg2.extensions"

    def __repr__(self):
        raise AssertionError("repr must not be called for live resources")

    def __reduce__(self):
        raise AssertionError("pickling must not be attempted for live resources")


class ExplodingDict:
    """Fails loudly if blackbox tries to walk a module __dict__."""

    __module__ = "thirdparty.sentinel"

    @property
    def __dict__(self):
        raise AssertionError("module __dict__ must not be traversed")


class UnpicklableModule(types.ModuleType):
    def __reduce__(self):
        raise RuntimeError("module cannot be pickled")


class Unpicklable:
    __module__ = "thirdparty.unpicklable"

    def __reduce__(self):
        raise RuntimeError("object cannot be pickled")


def _artifact(save_dir: Path, name: str) -> Path:
    matches = sorted(save_dir.rglob(name))
    assert len(matches) == 1
    return matches[0]


def _nested_dict(depth: int, *, leaf: object | None = None) -> dict:
    root: dict = {}
    current = root
    for _ in range(depth):
        child: dict = {}
        current["next"] = child
        current = child
    if leaf is not None:
        current["leaf"] = leaf
    return root


def test_psycopg2_connection_is_never_probed():
    live = connection()

    assert _contains_runtime_local_objects({"arg": live}) is True

    sanitized = _sanitize_for_pickle({"arg": live}, on_serialize_error="silent")
    marker = sanitized["arg"]
    assert marker["__blackbox_unserializable__"] is True
    assert marker["repr"] == "<psycopg2.extensions.connection>"
    assert "excluded from serialization" in marker["reason"]

    plain = _to_plain_data({"arg": live})
    assert plain["arg"]["repr"] == "<psycopg2.extensions.connection>"

    assert (
        _fallback_unserializable_marker(live, "test")["repr"]
        == "<psycopg2.extensions.connection>"
    )

    payload = {"arg": live}
    assert _neutralize_runtime_bound_objects(payload) is payload


def test_psycopg2_connection_guard_respects_raise_mode():
    live = connection()

    with pytest.raises(RuntimeError, match="excluded from serialization"):
        _sanitize_for_pickle({"arg": live}, on_serialize_error="raise")


def test_module_attributes_are_not_traversed():
    module = types.ModuleType("thirdparty.fake_module")
    module.sentinel = ExplodingDict()

    class Wrapper:
        __module__ = "thirdparty.wrapper"

        def __init__(self):
            self.module = module
            self.name = "wrapper"

    wrapper = Wrapper()

    assert _contains_runtime_local_objects({"arg": wrapper}) is False

    plain = _to_plain_data({"arg": wrapper})
    module_marker = plain["arg"]["module"]
    assert module_marker["__blackbox_unserializable__"] is True
    assert "module object" in module_marker["reason"]

    assert _neutralize_runtime_bound_objects(wrapper) is wrapper

    main_module = types.ModuleType("__main__")
    assert _contains_runtime_local_objects(main_module) is True


def test_unserializable_module_becomes_marker_without_dict_traversal():
    module = UnpicklableModule("thirdparty.bad_module")
    module.sentinel = ExplodingDict()

    sanitized = _sanitize_for_pickle({"arg": module}, on_serialize_error="silent")
    marker = sanitized["arg"]

    assert marker["__blackbox_unserializable__"] is True
    assert "module object remained unserializable" in marker["reason"]


def test_traversal_depth_limit_is_enforced():
    deep = _nested_dict(_MAX_INTROSPECTION_DEPTH + 20)

    assert _contains_runtime_local_objects(deep) is True

    plain = _to_plain_data(deep)
    node = plain
    for _ in range(_MAX_INTROSPECTION_DEPTH):
        node = node["next"]
    assert node["__blackbox_unserializable__"] is True
    assert "traversal depth limit" in node["reason"]

    assert _neutralize_runtime_bound_objects(deep) is deep


def test_sanitize_stops_at_traversal_depth_limit():
    deep = _nested_dict(_MAX_INTROSPECTION_DEPTH + 20, leaf=Unpicklable())

    sanitized = _sanitize_for_pickle(deep, on_serialize_error="silent")
    node = sanitized
    for _ in range(_MAX_INTROSPECTION_DEPTH):
        node = node["next"]
    assert node["__blackbox_unserializable__"] is True
    assert "traversal depth limit" in node["reason"]


def test_decorated_call_saves_artifacts_for_live_connection(tmp_path):
    live = connection()

    @blackbox(save_dir=str(tmp_path), capture_locals=False, on_serialize_error="silent")
    def identity(value):
        return value

    assert identity(live) is live

    input_data = load_pickle(str(_artifact(tmp_path, "input.pkl")))
    output_data = load_pickle(str(_artifact(tmp_path, "output.pkl")))

    assert input_data["args"][0]["__blackbox_unserializable__"] is True
    assert output_data["result"]["__blackbox_unserializable__"] is True
    assert output_data["result"]["repr"] == "<psycopg2.extensions.connection>"
