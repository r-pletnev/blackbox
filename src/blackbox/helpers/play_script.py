"""Загрузка снимка для генерации play-скрипта.

Читает артефакт (input.pkl / output.pkl / meta.json), а при наличии —
и вложенные вызовы из meta.json сессии, чтобы восстановить результаты
функций, которые вызывались внутри исследуемой.
"""

from __future__ import annotations

import ast
import base64
import builtins
import json
import pickle
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    from ..io import load_pickle
except ImportError:
    from blackbox.io import load_pickle


@dataclass
class NestedCall:
    """Один вложенный вызов, для которого есть собственный артефакт."""

    function: str
    module: str | None
    args: tuple
    kwargs: dict
    result: Any
    locals: dict
    depth: int


@dataclass
class SnapshotBundle:
    """Всё, что нужно генератору для одного снимка."""

    artifact_dir: Path
    function_name: str
    function_source: str | None
    module: str | None
    is_async: bool
    args: tuple
    kwargs: dict
    result: Any
    locals: dict = field(default_factory=dict)
    nested_calls: list[NestedCall] = field(default_factory=list)
    call_id: str | None = None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _resolve_artifact_dir(snapshot_path: str) -> Path:
    path = Path(snapshot_path)
    if path.is_file():
        if path.name == "output.pkl":
            return path.parent
        raise ValueError(
            f"Ожидался snapshot_path = папка артефакта или output.pkl, получено: {path}"
        )
    if not (path / "output.pkl").exists():
        raise FileNotFoundError(f"В {path} нет output.pkl — это не папка артефакта")
    return path


def _find_session_meta(artifact_dir: Path) -> Path | None:
    """Папка артефакта лежит внутри <session>/artifacts/..., ищем meta.json сессии."""
    for parent in artifact_dir.parents:
        candidate = parent / "meta.json"
        if candidate.exists() and (parent / "sessions").exists() is False:
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if "calls" in data:
                return candidate
    return None


def _load_nested_calls(artifact_dir: Path, call_id: str | None) -> list[NestedCall]:
    if not call_id:
        return []
    session_meta_path = _find_session_meta(artifact_dir)
    if session_meta_path is None:
        return []

    session_meta = _read_json(session_meta_path)
    session_dir = session_meta_path.parent
    nested: list[NestedCall] = []
    for call in session_meta.get("calls", []):
        if call.get("parentCallId") != call_id:
            continue
        child_dir = session_dir / call.get("artifactDir", "")
        if not (child_dir / "output.pkl").exists():
            continue
        input_data = load_pickle(str(child_dir / "input.pkl")) or {}
        output_data = load_pickle(str(child_dir / "output.pkl")) or {}
        nested.append(
            NestedCall(
                function=call.get("function") or "",
                module=call.get("module"),
                args=tuple(input_data.get("args", ())),
                kwargs=dict(input_data.get("kwargs", {})),
                result=output_data.get("result"),
                locals=output_data.get("locals") or {},
                depth=call.get("depth", 0),
            )
        )
    return nested

def load_snapshot(snapshot_path: str) -> SnapshotBundle:
    """Собрать данные снимка: функция, аргументы, результат, locals, вложенные вызовы."""
    artifact_dir = _resolve_artifact_dir(snapshot_path)
    meta = _read_json(artifact_dir / "meta.json")
    output_data = load_pickle(str(artifact_dir / "output.pkl")) or {}
    input_data = load_pickle(str(artifact_dir / "input.pkl")) or {}
    blackbox_meta = output_data.get("__blackbox__") or {}

    return SnapshotBundle(
        artifact_dir=artifact_dir,
        function_name=meta.get("function") or "",
        function_source=blackbox_meta.get("function_source"),
        module=meta.get("module"),
        is_async=bool(meta.get("isAsync")),
        args=tuple(input_data.get("args", ())),
        kwargs=dict(input_data.get("kwargs", {})),
        result=output_data.get("result"),
        locals=output_data.get("locals") or {},
        nested_calls=_load_nested_calls(artifact_dir, meta.get("callId")),
        call_id=meta.get("callId"),
    )


@dataclass
class CallSite:
    """Вызов внутри тела исследуемой функции.

    kind: "function" — свободное имя (helper, os.path.join),
          "method"   — метод на значении из снимка (self.conn.query).
    receiver используется только для подстановки мока.
    """

    name: str
    kind: str
    receiver: str | None
    lineno: int
    assigned_to: str | None

    @property
    def title(self) -> str:
        return f"{self.receiver}.{self.name}" if self.receiver else self.name


@dataclass
class MockPlan:
    """Мок одной вызываемой функции и восстановленные из снимка результаты."""

    name: str
    kind: str
    receiver: str | None = None
    calls: list[tuple[tuple, dict, Any]] = field(default_factory=list)
    nested: bool = False
    argless: bool = False

    @property
    def resolved(self) -> bool:
        return bool(self.calls)

    def describe(self) -> str:
        title = f"{self.receiver}.{self.name}" if self.receiver else self.name
        if not self.resolved:
            return f"{title} -> NotImplementedError (результата в снимке нет)"
        return f"{title} -> {len(self.calls)} восстановленных вызовов"


def _function_def(source: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(_normalized_source(source))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise ValueError("в исходнике нет определения функции")


def _attribute_parts(node: ast.AST) -> list[str] | None:
    """Разложить имя или цепочку атрибутов: ``helper`` → ["helper"], ``os.path.join`` → [...]."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return list(reversed(parts))


def _is_module_root(name: str) -> bool:
    """Имя загруженного модуля верхнего уровня (``json``, ``os``), но не класс/константа."""
    return isinstance(sys.modules.get(name), ModuleType)


def _normalized_source(source: str) -> str:
    """Убрать отступ класса/метода: inspect.getsource возвращает метод с отступом."""
    return textwrap.dedent(source)


def _bound_names(func_node: ast.AST) -> set[str]:
    """Имена, которые уже есть локально: параметры, аргументы, цели присваиваний."""
    names: set[str] = set()
    args = func_node.args
    for group in (args.posonlyargs, args.args, args.kwonlyargs):
        names.update(item.arg for item in group)
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            names.add(extra.arg)

    for node in ast.walk(func_node):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(
            node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.comprehension, ast.NamedExpr)
        ):
            targets = [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def _assigned_target(func_node: ast.AST) -> dict[int, str]:
    """id(Call) -> имя переменной, в которую записан результат вызова."""
    mapping: dict[int, str] = {}
    for node in ast.walk(func_node):
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Call):
            continue
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        if target:
            mapping[id(value)] = target
    return mapping


def find_call_sites(function_source: str) -> list[CallSite]:
    """Найти все вызовы внутри тела, кроме builtins и локальных значений.

    Свободные имена (``helper``, ``os.path.join``, ``json.loads``) считаются
    вызываемыми функциями; методом считается атрибут на локальном значении
    (параметр или присвоенная переменная), например ``self.conn.query``.
    Вызовы на литералах/импортах по месту (``" ".join(...)``) пропускаются.
    """
    func_node = _function_def(function_source)
    local_names = _bound_names(func_node)
    assigned = _assigned_target(func_node)
    sites: list[CallSite] = []
    seen: set[tuple[str, str, str | None]] = set()

    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        target_name = assigned.get(id(node))
        parts = _attribute_parts(node.func)
        if parts is None:
            continue
        if len(parts) == 1:
            name = parts[0]
            if name in local_names or hasattr(builtins, name):
                continue
            site = CallSite(name, "function", None, node.lineno, target_name)
        else:
            receiver, name = ".".join(parts[:-1]), parts[-1]
            root = parts[0]
            if root in local_names:
                site = CallSite(name, "method", receiver, node.lineno, target_name)
            elif _is_module_root(root) and not hasattr(builtins, name):
                # ``json.loads``, ``os.path.join``: receiver — модуль, подменяем атрибут.
                site = CallSite(name, "function", receiver, node.lineno, target_name)
            else:
                continue
        key = (site.kind, site.name, site.receiver)
        if key in seen:
            continue
        seen.add(key)
        sites.append(site)
    return sites


def build_mock_plan(snapshot: SnapshotBundle) -> list[MockPlan]:
    """Сопоставить вызовы в теле с результатами из снимка.

    Сначала берём вложенные артефакты (max_depth > 1): они дают результат вместе
    с аргументами вызова. Если вложенных артефактов нет, а результат присвоен
    переменной, берём значение из locals — тогда мок отвечает на любой вызов.
    """
    if not snapshot.function_source:
        return []

    plans: list[MockPlan] = []
    for site in find_call_sites(snapshot.function_source):
        plan = MockPlan(site.name, site.kind, site.receiver)
        for call in snapshot.nested_calls:
            recorded = call.function == site.name and (
                site.kind == "method" or bool(site.receiver) or not site.assigned_to
            )
            if not recorded:
                continue
            plan.calls.append((tuple(call.args), dict(call.kwargs), call.result))
            plan.nested = True
        if not plan.calls and site.assigned_to and site.assigned_to in snapshot.locals:
            # Значение из locals, аргументы вызова в снимке не сохранились.
            plan.calls.append(((), {}, snapshot.locals[site.assigned_to]))
            plan.argless = True
        plans.append(plan)
    return plans


def _free_names(function_source: str) -> list[str]:
    """Имена из тела, которых нет локально и в builtins: их надо подтянуть из модуля."""
    func_node = _function_def(function_source)
    local_names = _bound_names(func_node)
    names: list[str] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in local_names or hasattr(builtins, node.id):
                continue
            if node.id not in names:
                names.append(node.id)
    return sorted(names)


def _literal(value: Any) -> str:
    """Читаемый литерал, иначе восстановление через pickle (repr не round-trip для объектов)."""
    try:
        text = repr(value)
        if len(text) <= 4000 and ast.literal_eval(text) == value:
            return text
    except Exception:  # noqa: BLE001 — сравнение/parse произвольного значения может упасть
        text = None
    try:
        blob = base64.b85encode(pickle.dumps(value)).decode("ascii")
    except Exception as exc:  # noqa: BLE001 — объект мог не сохраниться в pickle
        return f'_placeholder("{type(value).__name__}", "{exc}")'
    return f'_unpickle("{blob}")'


def _strip_receiver(args: tuple, kwargs: dict) -> tuple[tuple, dict]:
    """Для методов убрать получателя (self): его нет среди аргументов вызова."""
    args = tuple(args)
    kwargs = dict(kwargs)
    if args:
        return args[1:], kwargs
    kwargs.pop("self", None)
    return args, kwargs


def _render_results(mocks: list[MockPlan]) -> list[str]:
    lines: list[str] = []
    for mock in mocks:
        for args, kwargs, value in mock.calls:
            if mock.kind == "method":
                args, kwargs = _strip_receiver(args, kwargs)
            if mock.argless:
                lines.append(
                    "# аргументы вызова в снимке не сохранились, результат берётся "
                    f"из locals ({mock.describe()})"
                )
                lines.append(
                    f"_record({mock.name!r}, (), {{}}, {_literal(value)}, argless=True)"
                )
                continue
            lines.append(
                f"_record({mock.name!r}, {_literal(tuple(args))}, "
                f"{_literal(dict(kwargs))}, {_literal(value)})"
            )
    return lines


def _render_mocks(mocks: list[MockPlan], sites: list[CallSite]) -> list[str]:
    lines: list[str] = []
    plan_by_key = {(mock.kind, mock.name, mock.receiver): mock for mock in mocks}
    for site in sites:
        if site.kind == "function" and site.receiver is None:
            lines.extend(
                [
                    f"def {site.name}(*args, **kwargs):",
                    f'    return _lookup("{site.name}", args, kwargs)',
                    "",
                ]
            )
            continue
        mock = plan_by_key.get((site.kind, site.name, site.receiver))
        lines.extend(_render_runtime_mock(site, bool(mock and mock.resolved)))
    if any(site.kind == "method" or site.receiver for site in sites):
        lines.extend(_render_patch_helpers())
    return lines


def _safe_attr(value: str) -> str:
    return value if value.isidentifier() else ""


def _mock_function_name(mock: MockPlan) -> str:
    receiver = mock.receiver or ""
    return f"_mock_{receiver.replace('.', '_')}_{mock.name}" if receiver else f"_mock_{mock.name}"


def _render_runtime_mock(site: CallSite, resolved: bool) -> list[str]:
    """Мок, подменяемый в месте вызова: регистрируется в PATCH_TARGETS."""
    title = site.title
    name = _mock_function_name(MockPlan(site.name, site.kind, site.receiver))
    if resolved:
        body = [f'    return _lookup({site.name!r}, args, kwargs)']
    else:
        body = [
            (
                f'    raise NotImplementedError(f"{title}: {{args}}{{kwargs}} — '
                'результат этого вызова не найден в снимке")'
            )
        ]
    return [
        f"def {name}(*args, **kwargs):",
        *body,
        "",
        "",
        # Площадка хранит сам объект мока: имя модуля в момент вызова ещё неизвестно.
        f"PATCH_TARGETS.append(({site.name!r}, {site.receiver!r}, {name}))",
        "",
        "",
    ]


def _has_module_sites(sites: list[CallSite]) -> bool:
    """Есть ли вызовы через модуль (``json.loads``, ``os.path.join``)."""
    return any(site.receiver and site.kind == "function" for site in sites)


def _runtime_name_expr(module_name: str) -> str:
    """``__name__`` подставляется через _runtime_name, чтобы работал и python -I, и импорт."""
    return f'_runtime_name("{module_name}")'


def _render_patch_helpers() -> list[str]:
    return [
        "def _patch(values):",
        '    """Поставить моки: атрибуты в импортированных модулях, методы на объектах снимка."""',
        "    for value in values:",
        "        _patch_sites(value)",
        '        for attribute in getattr(value, "__dict__", {}).values():',
        "            _patch_sites(attribute)",
        "",
        "",
        "def _patch_modules():",
        '    """Заменить атрибуты модулей во всех загруженных модулях (json.loads и т.п.)."""',
        "    for module in list(sys.modules.values()):",
        "        if not isinstance(module, ModuleType):",
        "            continue",
        "        for (name, receiver, mock_func) in PATCH_TARGETS:",
        "            if receiver is None or not _module_matches(module, receiver):",
        "                continue",
        "            try:",
        "                setattr(module, name, mock_func)",
        "            except (AttributeError, TypeError):",
        "                pass",
        "",
        "",
        "def _module_matches(module, receiver):",
        '    """``json.loads`` → модуль json; ``os.path.join`` → модуль posixpath (он же os.path)."""',
        "    if module is sys.modules.get(receiver):",
        "        return True",
        "    parts = receiver.split(\".\")",
        "    target = sys.modules.get(parts[0])",
        "    for part in parts[1:]:",
        "        target = getattr(target, part, None)",
        "    return target is not None and target is module",
        "",
        "",
        "def _patch_sites(target):",
        "    if target is None:",
        "        return",
        "    for (name, receiver, mock_func) in PATCH_TARGETS:",
        "        if receiver != \"self.\" + name:",
        "            continue",
        "        try:",
        "            setattr(target, name, mock_func)",
        "        except (AttributeError, TypeError):",
        "            pass",
        "",
    ]


# ── Шаблон сгенерированного скрипта ──────────────────────────────────────────

_RUNTIME_HELPERS = '''

import base64
import pickle
from types import ModuleType

RESULTS = []
PATCH_TARGETS = []


class _NotRestored:
    """Заглушка вместо значения, которое не удалось восстановить из снимка."""

    def __init__(self, type_name, reason):
        self.type_name = type_name
        self.reason = reason

    def __repr__(self):
        return f"<{self.type_name} не восстановлено: {self.reason}>"

    def __getattr__(self, item):
        raise NotImplementedError(f"{self!r}: нет атрибута {item}")


def _placeholder(type_name, reason):
    return _NotRestored(type_name, reason)


def _unpickle(blob):
    """Восстановить значение из снимка (требуется модуль, где объявлен класс)."""
    return pickle.loads(base64.b85decode(blob.encode("ascii")))


def _record(name, args, kwargs, value, argless=False):
    RESULTS.append((name, args, kwargs, value, argless))


def _runtime_name(function_name):
    """Модуль, в чьём глобальном словаре лежит сгенерированный мок."""
    if not function_name.startswith("_mock_"):
        return function_name
    namespace = globals()
    return namespace.get("__name__") or "__main__"


for _path in SYS_PATH:
    if _path not in sys.path:
        sys.path.append(_path)


def _missing(name, args, kwargs):
    raise NotImplementedError(
        f"{name}{args}{kwargs}: результат этого вызова не найден в снимке {SNAPSHOT_DIR}"
    )


def _lookup(name, args, kwargs):
    """Найти записанный результат; форма вызова (позиционная или по именам) не важна."""
    wanted = list(args) + list(kwargs.values())
    fallback = None
    for entry_name, entry_args, entry_kwargs, entry_value, entry_argless in RESULTS:
        if entry_name != name:
            continue
        if entry_argless:
            fallback = entry_value
            continue
        if list(entry_args) + list(entry_kwargs.values()) == wanted:
            return entry_value
    if fallback is not None:
        # Аргументы вызова в снимке не сохранились: отдаём результат по имени.
        return fallback
    return _missing(name, args, kwargs)


def _bind_environment(names):
    """Подтянуть из исходного модуля константы, классы и импорты тела функции."""
    if not MODULE or MODULE == "__main__":
        return
    import importlib

    try:
        origin = importlib.import_module(MODULE)
    except Exception as exc:
        print(f"[play] модуль {MODULE} не загружен: {exc}", file=sys.stderr)
        return
    for name in names:
        if name in globals():
            continue
        if hasattr(origin, name):
            globals()[name] = getattr(origin, name)
'''


def render_play_script(bundle: SnapshotBundle) -> str:
    """Собрать текст самодостаточного скрипта."""
    if not bundle.function_source:
        raise ValueError(
            f"В снимке {bundle.artifact_dir} нет исходника функции: "
            "передайте function_source явно"
        )

    mocks = build_mock_plan(bundle)
    sites = find_call_sites(bundle.function_source)
    body = _normalized_source(bundle.function_source).rstrip()
    function_name = bundle.function_name or _function_def(body).name
    local_names = {
        site.name for site in sites if site.kind == "function" and site.receiver is None
    }
    free_names = [name for name in _free_names(body) if name not in local_names]
    has_patches = any(site.kind == "method" or site.receiver for site in sites)

    lines = [
        "#!/usr/bin/env python3",
        f'"""Play-скрипт: {function_name}',
        "",
        "Сгенерирован blackbox из снимка:",
        f"    {bundle.artifact_dir}",
        "",
        f"Запуск: python play_{function_name}.py",
        "Моки возвращают результаты, восстановленные из снимка.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import sys",
        "",
        f"SNAPSHOT_DIR = {_literal(str(bundle.artifact_dir))}",
        f"MODULE = {_literal(bundle.module or '')}",
        f"FUNCTION = {_literal(function_name)}",
        f"SYS_PATH = {_literal(_snapshot_sys_path(bundle))}",
        f"FREE_NAMES = {_literal(free_names)}",
        _RUNTIME_HELPERS,
        "",
        "# ── Результаты вызовов, восстановленные из снимка ────────────────────",
        *_render_results(mocks),
    ]

    mock_lines = _render_mocks(mocks, sites)
    if mock_lines:
        lines.extend(["", "# ── Моки ─────────────────────────────────────────────────────────────", *mock_lines])

    lines.extend(
        [
            "",
            "# ── Тело исследуемой функции ─────────────────────────────────────────",
            body,
            "",
            "",
            f"ARGS = {_literal(list(bundle.args))}",
            f"KWARGS = {_literal(dict(bundle.kwargs))}",
            "",
            "",
            'if __name__ == "__main__":',
            "    _bind_environment(FREE_NAMES)",
        ]
    )
    if has_patches or _has_module_sites(sites):
        lines.append("    _patch_modules()")
    if has_patches:
        lines.append("    _patch([*ARGS, *KWARGS.values()])")
    if bundle.is_async:
        lines.extend(
            [
                "    import asyncio",
                "",
                f"    RESULT = asyncio.run({function_name}(*ARGS, **KWARGS))",
            ]
        )
    else:
        lines.append(f"    RESULT = {function_name}(*ARGS, **KWARGS)")
    lines.append('    print("результат:", RESULT)')
    lines.append("")
    return "\n".join(lines)


def _snapshot_sys_path(bundle: SnapshotBundle) -> list[str]:
    meta = _read_json(bundle.artifact_dir / "meta.json")
    raw = meta.get("sysPath") or []
    dirs = [item for item in raw if item and Path(item).is_dir()]
    source_path = meta.get("sourcePath")
    if source_path:
        parent = str(Path(source_path).parent)
        if Path(parent).is_dir() and parent not in dirs:
            dirs.insert(0, parent)
    return dirs


def generate_play_script(
    snapshot_path: str,
    function_name: str | None = None,
    function_source: str | None = None,
    output_dir: str | None = None,
) -> Path:
    """Создать play-скрипт для снимка и вернуть путь к нему."""
    bundle = load_snapshot(snapshot_path)
    if function_name:
        bundle.function_name = function_name
    if function_source:
        bundle.function_source = function_source

    script = render_play_script(bundle)
    play_id = bundle.call_id or bundle.artifact_dir.name
    target_dir = (
        Path(output_dir)
        if output_dir
        else Path.cwd() / "plays" / f"{bundle.function_name}-{play_id}"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"play_{bundle.function_name}.py"
    path.write_text(script, encoding="utf-8")
    return path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Показать снимок или собрать play-скрипт")
    parser.add_argument("snapshot_path")
    parser.add_argument("--output-dir", default=None, help="куда положить play-скрипт")
    parser.add_argument("--function-name", default=None, help="переопределить имя функции")
    parser.add_argument("--function-source", default=None, help="путь к файлу с исходником функции")
    args = parser.parse_args()

    if args.output_dir or args.function_source:
        source = (
            Path(args.function_source).read_text(encoding="utf-8")
            if args.function_source
            else None
        )
        print(
            generate_play_script(
                args.snapshot_path,
                function_name=args.function_name,
                function_source=source,
                output_dir=args.output_dir,
            )
        )
        return

    bundle = load_snapshot(args.snapshot_path)
    print(f"функция: {bundle.function_name} (module={bundle.module}, async={bundle.is_async})")
    print(f"source: {'есть' if bundle.function_source else 'нет'}")
    print(f"args: {bundle.args}")
    print(f"kwargs: {bundle.kwargs}")
    print(f"result: {bundle.result!r}")
    print(f"locals: {sorted(bundle.locals)}")
    mocks = build_mock_plan(bundle)
    print(f"вложенных вызовов: {len(bundle.nested_calls)}, моков: {len(mocks)}")
    for mock in mocks:
        print(f"  - {mock.describe()}")


if __name__ == "__main__":
    main()
