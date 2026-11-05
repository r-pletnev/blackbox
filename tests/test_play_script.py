import subprocess
import sys

import pytest

from blackbox import blackbox
from blackbox.helpers.play_script import (
    build_mock_plan,
    find_call_sites,
    generate_play_script,
    load_snapshot,
)

SESSION_ENV_VARS = ("BLACKBOX_SESSION_ID", "BLACKBOX_SESSION_DIR")


@pytest.fixture(autouse=True)
def clean_blackbox_session_env(monkeypatch):
    for name in SESSION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    """Сценарий: work вызывает helper трижды и other один раз, max_depth=4."""
    module_dir = tmp_path / "pkg"
    module_dir.mkdir()
    (module_dir / "scenario_mod.py").write_text(
        "def helper(x):\n"
        "    return x * 10\n"
        "\n"
        "def other(a, b):\n"
        "    return {'sum': a + b}\n"
        "\n"
        "def work(items):\n"
        "    out = [helper(i) for i in items]\n"
        "    return other(len(out), sum(out))\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(module_dir))
    sys.modules.pop("scenario_mod", None)

    from scenario_mod import work

    save_dir = tmp_path / "snapshots"
    blackbox(save_dir=str(save_dir), capture_locals=True, max_depth=4)(
        work
    )([1, 2, 3])

    artifact_dir = next(save_dir.rglob("work/*/output.pkl")).parent
    return artifact_dir


def test_load_snapshot_reads_arguments_result_and_locals(scenario):
    bundle = load_snapshot(str(scenario))

    assert bundle.function_name == "work"
    assert bundle.args == ([1, 2, 3],)
    assert bundle.result == {"sum": 63}
    assert bundle.locals["out"] == [10, 20, 30]
    assert bundle.function_source is not None
    assert "def work(items)" in bundle.function_source


def test_load_snapshot_collects_nested_calls(scenario):
    bundle = load_snapshot(str(scenario))

    by_name = {}
    for call in bundle.nested_calls:
        by_name.setdefault(call.function, []).append(call)

    assert [call.result for call in by_name["helper"]] == [10, 20, 30]
    assert [call.kwargs for call in by_name["helper"]] == [{"x": 1}, {"x": 2}, {"x": 3}]
    assert by_name["other"][0].kwargs == {"a": 3, "b": 60}
    assert by_name["other"][0].result == {"sum": 63}


def test_find_call_sites_skips_builtins_and_locals():
    source = (
        "def fn(items, cfg):\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        total += helper(item, k=cfg.limit())\n"
        "    text = ' x '.join(items)\n"
        "    if len(items):\n"
        "        report(total)\n"
        "    return total\n"
    )

    sites = find_call_sites(source)
    by_name = {site.name: site for site in sites}

    assert set(by_name) == {"helper", "limit", "report"}
    assert by_name["helper"].kind == "function"
    assert by_name["limit"].kind == "method"
    assert by_name["limit"].receiver == "cfg"
    assert "join" not in by_name


def test_find_call_sites_records_assignment_target():
    source = "def fn(items):\n    value = helper(items)\n    return value\n"

    (site,) = find_call_sites(source)

    assert site.name == "helper"
    assert site.assigned_to == "value"


def test_build_mock_plan_uses_nested_artifacts(scenario):
    bundle = load_snapshot(str(scenario))

    plans = {plan.name: plan for plan in build_mock_plan(bundle)}

    assert plans["helper"].resolved
    assert [call[2] for call in plans["helper"].calls] == [10, 20, 30]
    assert [call[1] for call in plans["helper"].calls] == [{"x": 1}, {"x": 2}, {"x": 3}]
    assert [call[2] for call in plans["other"].calls] == [{"sum": 63}]


def test_build_mock_plan_falls_back_to_parent_locals(tmp_path):
    """max_depth=1: вложенных артефактов нет, значение берём из locals родителя."""
    module_dir = tmp_path / "pkg"
    module_dir.mkdir()
    (module_dir / "fallback_mod.py").write_text(
        "def inner(x):\n"
        "    return x + 100\n"
        "\n"
        "def outer(items):\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        partial = inner(item)\n"
        "        total += partial\n"
        "    return total\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(module_dir))
    sys.modules.pop("fallback_mod", None)
    from fallback_mod import outer

    save_dir = tmp_path / "snapshots"
    blackbox(save_dir=str(save_dir), capture_locals=True)(outer)([1, 2])
    artifact_dir = next(save_dir.rglob("outer/*/output.pkl")).parent
    bundle = load_snapshot(str(artifact_dir))

    plans = {plan.name: plan for plan in build_mock_plan(bundle)}

    assert plans["inner"].resolved
    assert plans["inner"].calls == [((), {}, 102)]


def test_build_mock_plan_marks_unresolved_calls(scenario):
    bundle = load_snapshot(str(scenario))
    bundle.function_source = (
        "def work(items):\n"
        "    stamp()\n"
        "    return helper(items)\n"
    )

    plans = {plan.name: plan for plan in build_mock_plan(bundle)}

    assert plans["stamp"].resolved is False
    assert "NotImplementedError" in plans["stamp"].describe()


def test_load_snapshot_rejects_non_artifact_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_snapshot(str(tmp_path))


def test_load_snapshot_works_without_nested_artifacts(tmp_path):
    """max_depth=1: вложенных артефактов нет, но снимок читается."""
    module_dir = tmp_path / "pkg"
    module_dir.mkdir()
    (module_dir / "flat_mod.py").write_text(
        "def inner(x):\n"
        "    return x + 1\n"
        "\n"
        "def outer(items):\n"
        "    return [inner(i) for i in items]\n",
        encoding="utf-8",
    )
    import sys as _sys

    _sys.path.insert(0, str(module_dir))
    _sys.modules.pop("flat_mod", None)
    from flat_mod import outer

    save_dir = tmp_path / "snapshots"
    blackbox(save_dir=str(save_dir), capture_locals=True)(outer)([1, 2])

    artifact_dir = next(save_dir.rglob("outer/*/output.pkl")).parent
    bundle = load_snapshot(str(artifact_dir))

    assert bundle.function_name == "outer"
    assert bundle.nested_calls == []
    assert bundle.locals["items"] == [1, 2]


def test_generate_play_script_writes_runnable_script(scenario, tmp_path):
    out_dir = tmp_path / "plays"
    path = generate_play_script(str(scenario), output_dir=str(out_dir))

    assert path == out_dir / "play_work.py"
    text = path.read_text(encoding="utf-8")
    assert "def work(items):" in text
    assert "def helper(*args, **kwargs):" in text
    assert "RESULT = work(*ARGS, **KWARGS)" in text
    assert "import blackbox" not in text and "from blackbox" not in text


def test_generated_script_runs_without_blackbox(scenario, tmp_path):
    path = generate_play_script(str(scenario), output_dir=str(tmp_path / "plays"))

    completed = subprocess.run(
        [sys.executable, "-I", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "результат: {'sum': 63}" in completed.stdout


def test_generated_script_raises_for_unrecorded_call(scenario, tmp_path):
    source = (
        "def work(items):\n"
        "    stamp()\n"
        "    return other(len(items), sum(items))\n"
    )
    path = generate_play_script(
        str(scenario),
        function_source=source,
        output_dir=str(tmp_path / "plays"),
    )

    completed = subprocess.run(
        [sys.executable, "-I", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )

    assert completed.returncode != 0
    assert "NotImplementedError" in completed.stderr
    assert "stamp" in completed.stderr


def test_generated_script_mocks_methods_on_objects(tmp_path):
    """Метод на сохранённом объекте: мок ставится на атрибут, объект в аргументах восстанавливается."""
    module_dir = tmp_path / "pkg"
    module_dir.mkdir()
    (module_dir / "svc_mod.py").write_text(
        "class Client:\n"
        "    def query(self, sql):\n"
        "        return f'rows for {sql}'\n"
        "\n"
        "class Service:\n"
        "    def __init__(self):\n"
        "        self.conn = Client()\n"
        "        self.name = 'svc'\n"
        "\n"
        "    def fetch(self, sql):\n"
        "        marker = self.conn.query(sql)\n"
        "        return {'name': self.name, 'marker': marker}\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(module_dir))
    sys.modules.pop("svc_mod", None)
    from svc_mod import Service

    save_dir = tmp_path / "snapshots"
    blackbox(save_dir=str(save_dir), capture_locals=True, max_depth=4)(Service.fetch)(
        Service(), "select 1"
    )
    artifact_dir = next(save_dir.rglob("fetch/*/output.pkl")).parent

    path = generate_play_script(str(artifact_dir), output_dir=str(tmp_path / "plays"))
    completed = subprocess.run(
        [sys.executable, "-I", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "'marker': 'rows for select 1'" in completed.stdout
    generated = path.read_text(encoding="utf-8")
    assert "_mock_self_conn_query" in generated
    assert "PATCH_TARGETS.append(('query', 'self.conn'" in generated


def test_find_call_sites_handles_indented_method_source():
    source = "    def fetch(self, sql):\n        return self.conn.query(sql)\n"

    sites = find_call_sites(source)

    assert [(site.kind, site.receiver, site.name) for site in sites] == [
        ("method", "self.conn", "query")
    ]


@pytest.fixture
def module_call_scenario(tmp_path, monkeypatch):
    """Функция вызывает json.loads и os.path.join — вызовы через модуль, не через снимок."""
    module_dir = tmp_path / "modsrc"
    module_dir.mkdir()
    (module_dir / "mod_calls.py").write_text(
        "import json\n"
        "import os\n"
        "\n"
        "def parse(raw):\n"
        "    data = json.loads(raw)\n"
        '    path = os.path.join("out", data["name"])\n'
        '    return {"data": data, "path": path}\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(module_dir))
    sys.modules.pop("mod_calls", None)
    from mod_calls import parse

    save_dir = tmp_path / "snapshots"
    blackbox(save_dir=str(save_dir), capture_locals=True, max_depth=4)(parse)(
        '{"name": "report"}'
    )
    artifact_dir = next(save_dir.rglob("parse/*/output.pkl")).parent
    return artifact_dir, module_dir


def test_module_level_calls_become_mocks(module_call_scenario):
    artifact_dir, _ = module_call_scenario

    path = generate_play_script(str(artifact_dir), output_dir=str(artifact_dir / "plays"))
    generated = path.read_text(encoding="utf-8")

    assert "_mock_json_loads" in generated
    assert "_mock_os_path_join" in generated
    assert "PATCH_TARGETS.append(('loads', 'json'" in generated
    assert "PATCH_TARGETS.append(('join', 'os.path'" in generated

    completed = subprocess.run(
        [sys.executable, "-I", str(path)],
        capture_output=True,
        text=True,
        cwd=str(artifact_dir),
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "'path': 'out/report'" in completed.stdout


def test_argless_result_is_marked_and_lookup_falls_back(module_call_scenario):
    """Аргументы вызова неизвестны: результат помечается argless и доступен по имени."""
    artifact_dir, _ = module_call_scenario

    path = generate_play_script(str(artifact_dir), output_dir=str(artifact_dir / "plays"))
    generated = path.read_text(encoding="utf-8")

    assert "argless=True" in generated


def test_deep_receiver_object_method_is_mocked(tmp_path, monkeypatch):
    """Метод на объекте двумя уровнями вглубь (self.client.conn.query)."""
    module_dir = tmp_path / "deepsrc"
    module_dir.mkdir()
    (module_dir / "deep_mod.py").write_text(
        "class Conn:\n"
        "    def query(self, sql):\n"
        '        return f"rows for {sql}"\n'
        "\n"
        "class Client:\n"
        "    def __init__(self):\n"
        "        self.conn = Conn()\n"
        "\n"
        "class Service:\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n"
        "        self.client = Client()\n"
        "\n"
        "    def fetch(self, sql):\n"
        "        marker = self.client.conn.query(sql)\n"
        '        return {"name": self.name, "marker": marker}\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(module_dir))
    sys.modules.pop("deep_mod", None)
    from deep_mod import Service

    save_dir = tmp_path / "snapshots"
    blackbox(save_dir=str(save_dir), capture_locals=True, max_depth=4)(Service.fetch)(
        Service("svc"), "select 1"
    )
    artifact_dir = next(save_dir.rglob("fetch/*/output.pkl")).parent

    path = generate_play_script(str(artifact_dir), output_dir=str(tmp_path / "plays"))
    completed = subprocess.run(
        [sys.executable, "-I", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "'marker': 'rows for select 1'" in completed.stdout


def test_unresolved_mock_raises_with_name_and_args(tmp_path, monkeypatch):
    """Если результата нет, мок обязан бросить NotImplementedError с именем и аргументами."""
    module_dir = tmp_path / "unressrc"
    module_dir.mkdir()
    (module_dir / "unres_mod.py").write_text(
        "def helper(value):\n"
        "    return value\n"
        "\n"
        "def pipeline(raw):\n"
        '    return {"done": helper(raw)}\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(module_dir))
    sys.modules.pop("unres_mod", None)
    from unres_mod import pipeline

    save_dir = tmp_path / "snapshots"
    blackbox(save_dir=str(save_dir), capture_locals=True, max_depth=4)(pipeline)("data")
    artifact_dir = next(save_dir.rglob("pipeline/*/output.pkl")).parent

    path = generate_play_script(str(artifact_dir), output_dir=str(tmp_path / "plays"))
    # Ломаем запись результата: мок не должен найти значение и обязан сообщить об этом.
    broken = path.read_text(encoding="utf-8").replace("_record('helper'", "_record('нет_такого'")
    path.write_text(broken, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-I", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )

    assert completed.returncode != 0
    assert "NotImplementedError" in completed.stderr
    assert "helper" in completed.stderr
    assert "'data'" in completed.stderr
