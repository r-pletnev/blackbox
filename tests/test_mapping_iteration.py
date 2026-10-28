import json
from collections.abc import Iterator, MutableMapping
from pathlib import Path

import pytest

from blackbox import blackbox
from blackbox.io import (
    _contains_runtime_local_objects,
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


class ThreadUnsafeMapping(MutableMapping[str, str]):
    """Mimics urllib3._collections.RecentlyUsedContainer."""

    __module__ = "thirdparty.collections"

    def __init__(self, values: dict[str, str] | None = None):
        self._values = dict(values or {})

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[str]:
        raise NotImplementedError(
            "Iteration over this class is unlikely to be threadsafe."
        )


class PartiallyIterableMapping(MutableMapping[str, int]):
    """Mapping that yields one item and then fails mid-iteration."""

    __module__ = "thirdparty.collections"

    def __iter__(self):
        yield "kept"
        raise RuntimeError("iterator failed")

    def __getitem__(self, key: str) -> int:
        return 1

    def __setitem__(self, key: str, value: int) -> None:
        pass

    def __delitem__(self, key: str) -> None:
        pass

    def __len__(self) -> int:
        return 1


def _session_dir(save_dir: Path) -> Path:
    sessions_dir = save_dir / "sessions"
    assert sessions_dir.exists()
    session_dirs = sorted(path for path in sessions_dir.iterdir() if path.is_dir())
    assert len(session_dirs) == 1
    return session_dirs[0]


def _call_dir(save_dir: Path, function_name: str) -> Path:
    session_dir = _session_dir(save_dir)
    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    calls = [call for call in meta["calls"] if call["function"] == function_name]
    assert len(calls) == 1
    return session_dir / calls[0]["artifactDir"]


def test_scan_and_sanitize_handle_uniterable_mapping():
    broken = ThreadUnsafeMapping({"pool": "value"})

    assert _contains_runtime_local_objects({"arg": broken}) is True

    sanitized = _sanitize_for_pickle({"arg": broken}, on_serialize_error="silent")
    marker = sanitized["arg"]

    assert marker["__blackbox_unserializable__"] is True
    assert marker["type"] == "ThreadUnsafeMapping"
    assert marker["module"] == "thirdparty.collections"
    assert "NotImplementedError" in marker["reason"]

    plain = _to_plain_data({"arg": broken})
    assert plain["arg"]["__blackbox_unserializable__"] is True

    wrapped = {"arg": broken}
    assert _neutralize_runtime_bound_objects(wrapped) is wrapped


def test_decorated_call_captures_uniterable_mapping_in_args_and_result(tmp_path):
    broken = ThreadUnsafeMapping({"pool": "value"})

    @blackbox(save_dir=str(tmp_path), capture_locals=False, on_serialize_error="silent")
    def identity(value):
        return value

    assert identity(broken) is broken

    call_dir = _call_dir(tmp_path, "identity")
    input_data = load_pickle(str(call_dir / "input.pkl"))
    output_data = load_pickle(str(call_dir / "output.pkl"))

    assert input_data["args"][0]["__blackbox_unserializable__"] is True
    assert output_data["result"]["__blackbox_unserializable__"] is True

    issues = output_data["__blackbox__"]["serialization_issues"]
    assert any("mapping iteration failed" in issue["reason"] for issue in issues)


def test_partially_iterable_mapping_keeps_items_and_adds_error_marker(tmp_path):
    partially_broken = PartiallyIterableMapping()

    @blackbox(save_dir=str(tmp_path), capture_locals=False, on_serialize_error="silent")
    def identity(value):
        return value

    assert identity(partially_broken) is partially_broken

    output_data = load_pickle(str(_call_dir(tmp_path, "identity") / "output.pkl"))
    result = output_data["result"]

    assert result["kept"] == 1
    error_marker = result["__blackbox_mapping_error__"]
    assert error_marker["__blackbox_unserializable__"] is True
    assert "RuntimeError" in error_marker["reason"]
