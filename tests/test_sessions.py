import asyncio
import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
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
    meta_path = session_dir / "meta.json"
    assert meta_path.exists()
    return session_dir, json.loads(meta_path.read_text(encoding="utf-8"))


def _calls_by_function(meta: dict) -> dict[str, dict]:
    return {call["function"]: call for call in meta["calls"]}


def _call_artifact_dir(session_dir: Path, call: dict) -> Path:
    artifact_dir = session_dir / call["artifactDir"]
    assert artifact_dir.exists()
    return artifact_dir


def _run_process_decorated_call(save_dir: str, queue) -> None:
    from blackbox import blackbox

    @blackbox(save_dir=save_dir, capture_locals=False)
    def process_step(value: int) -> int:
        return value + 10

    queue.put(process_step(5))


def test_sync_decorated_calls_share_one_session_and_artifact_tree(tmp_path):
    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def add(left: int, right: int) -> int:
        return left + right

    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def multiply(left: int, right: int) -> int:
        return left * right

    assert add(2, 3) == 5
    assert multiply(2, 3) == 6

    session_dir, meta = _load_session_meta(tmp_path)

    assert meta["schemaVersion"] == 1
    assert meta["sessionId"] == session_dir.name
    assert os.environ["BLACKBOX_SESSION_ID"] == meta["sessionId"]
    assert Path(os.environ["BLACKBOX_SESSION_DIR"]) == session_dir
    assert [call["function"] for call in meta["calls"]] == ["add", "multiply"]
    assert [call["sequence"] for call in meta["calls"]] == [1, 2]
    assert [call["parentCallId"] for call in meta["calls"]] == [None, None]
    assert [call["depth"] for call in meta["calls"]] == [0, 0]

    for call in meta["calls"]:
        artifact_dir = _call_artifact_dir(session_dir, call)
        assert (artifact_dir / "input.pkl").exists()
        assert (artifact_dir / "output.pkl").exists()

        call_meta = json.loads((artifact_dir / "meta.json").read_text(encoding="utf-8"))
        assert call_meta["sessionId"] == meta["sessionId"]
        assert call_meta["callId"] == call["id"]
        assert call_meta["sequence"] == call["sequence"]


def test_session_metadata_records_order_depth_and_parent_child(tmp_path):
    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def inner(value: int) -> int:
        return value + 1

    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def outer(value: int) -> int:
        return inner(value) * 2

    assert outer(10) == 22

    _, meta = _load_session_meta(tmp_path)
    calls = meta["calls"]
    calls_by_function = _calls_by_function(meta)
    outer_call = calls_by_function["outer"]
    inner_call = calls_by_function["inner"]

    assert [call["function"] for call in calls] == ["outer", "inner"]
    assert outer_call["sequence"] < inner_call["sequence"]
    assert outer_call["parentCallId"] is None
    assert outer_call["depth"] == 0
    assert inner_call["parentCallId"] == outer_call["id"]
    assert inner_call["depth"] == 1


def test_sync_and_async_decorated_calls_are_grouped_in_one_session(tmp_path):
    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def sync_step() -> str:
        return "sync"

    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    async def async_step() -> str:
        await asyncio.sleep(0)
        return "async"

    async def run_calls() -> None:
        assert sync_step() == "sync"
        assert await async_step() == "async"

    asyncio.run(run_calls())

    _, meta = _load_session_meta(tmp_path)
    calls_by_function = _calls_by_function(meta)

    assert [call["function"] for call in meta["calls"]] == ["sync_step", "async_step"]
    assert calls_by_function["sync_step"]["isAsync"] is False
    assert calls_by_function["async_step"]["isAsync"] is True


def test_threads_join_existing_session(tmp_path):
    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def seed_session() -> str:
        return "ready"

    barrier = threading.Barrier(3)

    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def threaded_step(value: int) -> int:
        barrier.wait(timeout=5)
        return threading.get_ident() + value

    assert seed_session() == "ready"

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(threaded_step, 1)
        second = executor.submit(threaded_step, 2)
        barrier.wait(timeout=5)
        first.result(timeout=5)
        second.result(timeout=5)

    _, meta = _load_session_meta(tmp_path)
    threaded_calls = [
        call for call in meta["calls"] if call["function"] == "threaded_step"
    ]

    assert len(meta["calls"]) == 3
    assert len(threaded_calls) == 2
    assert {call["parentCallId"] for call in threaded_calls} == {None}
    assert len({call["threadId"] for call in threaded_calls}) == 2


def test_processes_inherit_existing_session_from_environment(tmp_path):
    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def parent_step() -> str:
        return "parent"

    assert parent_step() == "parent"

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(
        target=_run_process_decorated_call,
        args=(str(tmp_path), queue),
    )
    process.start()
    process.join(timeout=10)

    assert process.exitcode == 0
    assert queue.get(timeout=1) == 15

    _, meta = _load_session_meta(tmp_path)
    calls_by_function = _calls_by_function(meta)

    assert [call["function"] for call in meta["calls"]] == [
        "parent_step",
        "process_step",
    ]
    assert calls_by_function["parent_step"]["pid"] == os.getpid()
    assert calls_by_function["process_step"]["pid"] == process.pid
    assert calls_by_function["process_step"]["parentCallId"] is None


def test_failed_call_records_status_and_does_not_leak_stack(tmp_path):
    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def fails() -> None:
        raise RuntimeError("boom")

    @blackbox(save_dir=str(tmp_path), capture_locals=False)
    def after_failure() -> str:
        return "ok"

    with pytest.raises(RuntimeError, match="boom"):
        fails()
    assert after_failure() == "ok"

    _, meta = _load_session_meta(tmp_path)
    calls_by_function = _calls_by_function(meta)
    failed_call = calls_by_function["fails"]
    next_call = calls_by_function["after_failure"]

    assert failed_call["status"] == "failed"
    assert failed_call["exception"] == "RuntimeError"
    assert next_call["status"] == "completed"
    assert next_call["parentCallId"] is None
    assert next_call["depth"] == 0
