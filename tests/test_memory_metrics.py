import json
from pathlib import Path

import pytest

from blackbox import blackbox


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
    return session_dir, json.loads((session_dir / "meta.json").read_text("utf-8"))


def _only_call_named(meta: dict, name: str) -> dict:
    calls = [call for call in meta["calls"] if call["function"] == name]
    assert len(calls) == 1
    return calls[0]


def _assert_memory_payload(memory: dict) -> None:
    assert memory["backend"]
    assert memory["pythonCurrentStartBytes"] is not None
    assert memory["pythonCurrentEndBytes"] is not None
    assert memory["pythonCurrentDeltaBytes"] == (
        memory["pythonCurrentEndBytes"] - memory["pythonCurrentStartBytes"]
    )
    assert memory["pythonPeakBytes"] is not None
    assert memory["processMaxRssStartBytes"] is not None
    assert memory["processMaxRssEndBytes"] is not None
    assert memory["processMaxRssDeltaBytes"] == (
        memory["processMaxRssEndBytes"] - memory["processMaxRssStartBytes"]
    )


def test_capture_memory_records_session_and_call_meta(tmp_path):
    @blackbox(save_dir=str(tmp_path), capture_locals=False, capture_memory=True)
    def allocate_values(count: int) -> int:
        values = [str(index) for index in range(count)]
        return len(values)

    assert allocate_values(100) == 100

    session_dir, session_meta = _load_session_meta(tmp_path)
    call = _only_call_named(session_meta, "allocate_values")
    _assert_memory_payload(call["memory"])

    call_meta_path = session_dir / call["artifactDir"] / "meta.json"
    call_meta = json.loads(call_meta_path.read_text("utf-8"))

    assert call_meta["memory"] == call["memory"]


def test_capture_memory_false_omits_memory_payload(tmp_path):
    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def plain_call() -> str:
        return "ok"

    assert plain_call() == "ok"

    session_dir, session_meta = _load_session_meta(tmp_path)
    call = _only_call_named(session_meta, "plain_call")
    assert "memory" not in call

    call_meta_path = session_dir / call["artifactDir"] / "meta.json"
    call_meta = json.loads(call_meta_path.read_text("utf-8"))
    assert "memory" not in call_meta


def test_capture_memory_records_nested_calls(tmp_path):
    def child(count: int) -> int:
        values = [object() for _ in range(count)]
        return len(values)

    @blackbox(
        save_dir=str(tmp_path),
        capture_locals=False,
        capture_memory=True,
        max_depth=2,
    )
    def root(count: int) -> int:
        return child(count)

    assert root(50) == 50

    _, session_meta = _load_session_meta(tmp_path)
    root_call = _only_call_named(session_meta, "root")
    child_call = _only_call_named(session_meta, "child")

    _assert_memory_payload(root_call["memory"])
    _assert_memory_payload(child_call["memory"])
    assert child_call["parentCallId"] == root_call["id"]
