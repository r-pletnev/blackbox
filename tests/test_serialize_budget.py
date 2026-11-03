import ssl
import types
from pathlib import Path

import pytest

from blackbox import blackbox
from blackbox import io as io_module
from blackbox.io import _sanitize_for_pickle, _try_serialize, load_pickle


class SharedLeaf:
    """Unserializable leaf that forces every parent probe to fail."""

    __module__ = "thirdparty.shared"

    def __reduce__(self):
        raise RuntimeError("object cannot be pickled")


class Endpoint:
    def __init__(self, leaf):
        self._manager = types.SimpleNamespace(pools={"leaf": leaf})


class Client:
    def __init__(self, leaf, ctx):
        self._endpoint = Endpoint(leaf)
        self.ctx = ctx


@pytest.fixture(autouse=True)
def clean_blackbox_session_env(monkeypatch):
    for name in ("BLACKBOX_SESSION_ID", "BLACKBOX_SESSION_DIR"):
        monkeypatch.delenv(name, raising=False)


def _payload(clients: int = 20) -> dict:
    leaf = SharedLeaf()
    ctx = ssl.create_default_context()
    return {"args": [Client(leaf, ctx) for _ in range(clients)]}


def _artifact(save_dir: Path, name: str) -> Path:
    matches = sorted(save_dir.rglob(name))
    assert len(matches) == 1
    return matches[0]


def _count_probes(monkeypatch) -> list:
    calls = []
    original = io_module._try_serialize

    def spy(obj, **kwargs):
        calls.append(obj)
        return original(obj, **kwargs)

    monkeypatch.setattr(io_module, "_try_serialize", spy)
    return calls


def test_probe_budget_bounds_serialization_attempts(monkeypatch):
    monkeypatch.setattr(io_module, "_MAX_SERIALIZE_PROBES", 5)
    calls = _count_probes(monkeypatch)

    sanitized = _sanitize_for_pickle(_payload(), on_serialize_error="silent")

    assert len(calls) <= 5
    assert sanitized["args"][0]["ctx"]["__blackbox_unserializable__"] is True


def test_ssl_context_is_never_probed(monkeypatch):
    monkeypatch.setattr(io_module, "_MAX_SERIALIZE_PROBES", 50)
    calls = _count_probes(monkeypatch)

    _sanitize_for_pickle({"ctx": ssl.create_default_context()}, on_serialize_error="silent")

    assert not any(isinstance(obj, ssl.SSLContext) for obj in calls)


def test_budget_exhaustion_reports_once(capsys):
    _sanitize_for_pickle(_payload(), on_serialize_error="warn")

    output = capsys.readouterr().out
    assert output.count("serialization probe budget exceeded") == 1


def test_budget_exhaustion_raises_in_raise_mode(monkeypatch):
    monkeypatch.setattr(io_module, "_MAX_SERIALIZE_PROBES", 1)

    with pytest.raises(RuntimeError, match="serialization probe budget exceeded"):
        _sanitize_for_pickle(_payload(), on_serialize_error="raise")


def test_single_probe_deadline_aborts_serialization():
    ok, reason = _try_serialize(_payload(), timeout=-1)

    assert ok is False
    assert "_SerializationDeadlineExceeded" in (reason or "")


def test_decorated_call_saves_and_loads_graph_with_ssl_context(tmp_path):
    payload = _payload(clients=3)

    @blackbox(save_dir=str(tmp_path), capture_locals=False, on_serialize_error="silent")
    def identity(value):
        return value

    assert identity(payload) is payload

    input_data = load_pickle(str(_artifact(tmp_path, "input.pkl")))
    output_data = load_pickle(str(_artifact(tmp_path, "output.pkl")))

    assert input_data["args"][0]["args"][0]["ctx"]["__blackbox_unserializable__"] is True
    assert output_data["result"]["args"][1]["ctx"]["__blackbox_unserializable__"] is True


def test_exhausted_child_is_marked_while_its_parent_keeps_other_fields(monkeypatch):
    monkeypatch.setattr(io_module, "_MAX_SERIALIZE_PROBES", 2)
    calls = _count_probes(monkeypatch)

    payload = {"outer": {"a": SharedLeaf(), "b": SharedLeaf()}}
    sanitized = _sanitize_for_pickle(payload, on_serialize_error="silent")

    assert len(calls) == 2
    outer = sanitized["outer"]
    assert set(outer) == {"a", "b"}
    assert isinstance(outer["a"], dict) and isinstance(outer["b"], dict)


def test_parameters_after_budget_are_walked_structurally(monkeypatch):
    monkeypatch.setattr(io_module, "_MAX_SERIALIZE_PROBES", 2)
    calls = _count_probes(monkeypatch)

    payload = {"bad": {"a": SharedLeaf(), "b": 1}, "cheap": {"value": 1}}
    sanitized = _sanitize_for_pickle(payload, on_serialize_error="silent")

    assert len(calls) == 2
    assert set(sanitized["bad"]) == {"a", "b"}
    assert sanitized["bad"]["b"] == 1
    assert sanitized["cheap"] == {"value": 1}


def test_object_denied_a_probe_keeps_its_other_fields(monkeypatch):
    monkeypatch.setattr(io_module, "_MAX_SERIALIZE_PROBES", 1)

    class connection:
        __module__ = "psycopg2.extensions"

    class Service:
        __module__ = "app.services"

        def __init__(self):
            self.conn = connection()
            self.name = "service"
            self.settings = {"retries": 3}

    # The shared leaf burns the budget first, so Service itself is denied a probe.
    payload = {"bad": SharedLeaf(), "self": Service()}
    sanitized = _sanitize_for_pickle(payload, on_serialize_error="silent")

    service = sanitized["self"]
    assert service["__object__"] == "Service"
    assert service["conn"]["__blackbox_unserializable__"] is True
    assert service["name"] == "service"
    assert service["settings"] == {"retries": 3}
