import asyncio
import json
import os
from pathlib import Path

import pytest

from blackbox import blackbox
from blackbox.io import load_pickle


SESSION_ENV_VARS = ("BLACKBOX_SESSION_ID", "BLACKBOX_SESSION_DIR")


@pytest.fixture(autouse=True)
def clean_blackbox_session_env(monkeypatch):
    for name in SESSION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _load_session_meta(save_dir: Path) -> tuple[Path, dict]:
    sessions_dir = save_dir / "sessions"
    assert sessions_dir.exists()
    session_dirs = sorted(path for path in sessions_dir.iterdir() if path.is_dir())
    assert len(session_dirs) == 1

    session_dir = session_dirs[0]
    meta_path = session_dir / "meta.json"
    assert meta_path.exists()
    return session_dir, json.loads(meta_path.read_text(encoding="utf-8"))


def _calls_named(meta: dict, name: str) -> list[dict]:
    return [call for call in meta["calls"] if call["function"] == name]


def _only_call_named(meta: dict, name: str) -> dict:
    calls = _calls_named(meta, name)
    assert len(calls) == 1
    return calls[0]


def _artifact_dir(session_dir: Path, call: dict) -> Path:
    path = session_dir / call["artifactDir"]
    assert path.exists()
    return path


def _output_result(session_dir: Path, call: dict):
    output_path = _artifact_dir(session_dir, call) / "output.pkl"
    assert output_path.exists()
    return load_pickle(str(output_path))["result"]


def test_max_depth_one_records_only_root_call(tmp_path):
    def helper(value: int) -> int:
        return value + 1

    @blackbox(save_dir=str(tmp_path), capture_locals=False, max_depth=1)
    def root(value: int) -> int:
        return helper(value) * 2

    assert root(10) == 22

    _, meta = _load_session_meta(tmp_path)

    assert [call["function"] for call in meta["calls"]] == ["root"]
    assert meta["calls"][0]["depth"] == 0
    assert meta["calls"][0]["parentCallId"] is None


def test_max_depth_two_records_direct_user_subcall(tmp_path):
    def direct_helper(value: int) -> int:
        return value + 1

    @blackbox(save_dir=str(tmp_path), capture_locals=False, max_depth=2)
    def root(value: int) -> int:
        return direct_helper(value) * 2

    assert root(10) == 22

    session_dir, meta = _load_session_meta(tmp_path)
    root_call = _only_call_named(meta, "root")
    helper_call = _only_call_named(meta, "direct_helper")

    assert [call["function"] for call in meta["calls"]] == ["root", "direct_helper"]
    assert root_call["sequence"] == 1
    assert helper_call["sequence"] == 2
    assert helper_call["parentCallId"] == root_call["id"]
    assert helper_call["depth"] == 1
    assert _output_result(session_dir, helper_call) == 11


def test_max_depth_two_skips_grandchild_user_subcall(tmp_path):
    def grandchild(value: int) -> int:
        return value + 1

    def direct_helper(value: int) -> int:
        return grandchild(value) * 2

    @blackbox(save_dir=str(tmp_path), capture_locals=False, max_depth=2)
    def root(value: int) -> int:
        return direct_helper(value) + 3

    assert root(10) == 25

    _, meta = _load_session_meta(tmp_path)

    assert [call["function"] for call in meta["calls"]] == ["root", "direct_helper"]
    assert not _calls_named(meta, "grandchild")


def test_max_depth_three_records_grandchild_parent_chain(tmp_path):
    def grandchild(value: int) -> int:
        return value + 1

    def direct_helper(value: int) -> int:
        return grandchild(value) * 2

    @blackbox(save_dir=str(tmp_path), capture_locals=False, max_depth=3)
    def root(value: int) -> int:
        return direct_helper(value) + 3

    assert root(10) == 25

    session_dir, meta = _load_session_meta(tmp_path)
    root_call = _only_call_named(meta, "root")
    helper_call = _only_call_named(meta, "direct_helper")
    grandchild_call = _only_call_named(meta, "grandchild")

    assert [call["function"] for call in meta["calls"]] == [
        "root",
        "direct_helper",
        "grandchild",
    ]
    assert helper_call["parentCallId"] == root_call["id"]
    assert grandchild_call["parentCallId"] == helper_call["id"]
    assert [root_call["depth"], helper_call["depth"], grandchild_call["depth"]] == [
        0,
        1,
        2,
    ]
    assert _output_result(session_dir, grandchild_call) == 11


def test_max_depth_records_recursive_calls_with_unique_sequences(tmp_path):
    def factorial(value: int) -> int:
        if value <= 1:
            return 1
        return value * factorial(value - 1)

    @blackbox(save_dir=str(tmp_path), capture_locals=False, max_depth=5)
    def root(value: int) -> int:
        return factorial(value)

    assert root(4) == 24

    _, meta = _load_session_meta(tmp_path)
    factorial_calls = _calls_named(meta, "factorial")
    sequences = [call["sequence"] for call in meta["calls"]]

    assert len(factorial_calls) == 4
    assert sequences == [1, 2, 3, 4, 5]
    assert len(set(sequences)) == len(sequences)
    assert [call["depth"] for call in factorial_calls] == [1, 2, 3, 4]


def test_max_depth_does_not_duplicate_explicitly_decorated_child(tmp_path):
    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def decorated_child(value: int) -> int:
        return value + 1

    @blackbox(save_dir=str(tmp_path), capture_locals=False, max_depth=2)
    def root(value: int) -> int:
        return decorated_child(value) * 2

    assert root(10) == 22

    _, meta = _load_session_meta(tmp_path)
    root_call = _only_call_named(meta, "root")
    child_call = _only_call_named(meta, "decorated_child")

    assert [call["function"] for call in meta["calls"]] == [
        "root",
        "decorated_child",
    ]
    assert child_call["parentCallId"] == root_call["id"]
    assert child_call["depth"] == 1


def test_max_depth_records_failed_nested_call(tmp_path):
    def broken_child() -> None:
        raise ValueError("nope")

    @blackbox(save_dir=str(tmp_path), capture_locals=False, max_depth=2)
    def root() -> None:
        broken_child()

    with pytest.raises(ValueError, match="nope"):
        root()

    _, meta = _load_session_meta(tmp_path)
    root_call = _only_call_named(meta, "root")
    child_call = _only_call_named(meta, "broken_child")

    assert root_call["status"] == "failed"
    assert root_call["exception"] == "ValueError"
    assert child_call["status"] == "failed"
    assert child_call["exception"] == "ValueError"
    assert child_call["parentCallId"] == root_call["id"]


def test_async_max_depth_warns_and_falls_back_to_root_capture(tmp_path, recwarn):
    def sync_helper(value: int) -> int:
        return value + 1

    @blackbox(save_dir=str(tmp_path), capture_locals=False, max_depth=2)
    async def async_root(value: int) -> int:
        await asyncio.sleep(0)
        return sync_helper(value) * 2

    assert asyncio.run(async_root(10)) == 22

    warnings = [
        warning
        for warning in recwarn
        if issubclass(warning.category, RuntimeWarning)
        and "max_depth" in str(warning.message)
        and "async" in str(warning.message).lower()
    ]
    assert warnings

    _, meta = _load_session_meta(tmp_path)

    assert [call["function"] for call in meta["calls"]] == ["async_root"]
    assert meta["calls"][0]["isAsync"] is True
    assert not _calls_named(meta, "sync_helper")
