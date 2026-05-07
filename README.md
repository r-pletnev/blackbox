# blackbox

`blackbox` — легковесная библиотека для записи артефактов вызова Python-функций.
Она сохраняет входные аргументы, результат выполнения, длительность вызова,
исключение и, при необходимости, локальные переменные функции.

Подходит для отладки, воспроизводимости вычислений и разбора сложных падений,
когда хочется получить "снимок" выполнения без ручного `print`-логирования.

## Возможности

- Декоратор `@blackbox(...)` для sync- и async-функций
- Сохранение входных данных и результата вызова
- Захват локальных переменных через `sys.settrace`
- Отдельные артефакты для каждого вызова
- Сохранение артефактов через `dill` по умолчанию
- Возможность переключиться на стандартный `pickle`
- Отдельный легкий `meta.json` для быстрой инспекции вызова
- Запись метаданных вызова, включая текущую git-ветку

## Установка

Установка:

```bash
pip install blackbox
```

Для локальной разработки:

```bash
uv sync
```

## Быстрый старт

```python
from blackbox import blackbox


@blackbox(save_dir="/tmp/blackbox", verbose=True)
def add(a: int, b: int) -> int:
    total = a + b
    return total


result = add(2, 3)
print(result)
```

После вызова будет создана директория вида:

```text
/tmp/blackbox/
  your_module.py/
    add/
      20260425_120102_123456_ab12cd34/
        input.pkl
        output.pkl
        meta.json
```

## Что сохраняется

`input.pkl`
- аргументы `args` и `kwargs`

`output.pkl`
- `result`
- `duration_ms`
- `exception`
- `locals`, если включен `capture_locals`

`meta.json`
- время создания артефакта
- git-ветка
- имя функции и модуля
- путь к исходному файлу функции
- флаг async/sync
- `title` снапшота
- хэш вызова
- роль артефакта
- тип исключения, если вызов завершился ошибкой
- список имен локальных переменных, которые удалось поймать

## Параметры декоратора

```python
blackbox(
    save_dir="/tmp/blackbox",
    save_input=True,
    save_output=True,
    capture_locals=True,
    verbose=False,
    serializer="dill",
    sanitize_hooks=None,
    on_serialize_error="warn",
    locals_strategy="settrace",
)
```

Описание параметров:

- `save_dir` — корневая директория для артефактов
- `save_input` — сохранять входные аргументы
- `save_output` — сохранять результат и метаданные выполнения
- `capture_locals` — захватывать локальные переменные функции
- `verbose` — печатать список сохраненных файлов
- `serializer` — backend сериализации: `"dill"` или `"pickle"`
- `sanitize_hooks` — пользовательские функции санации для нестандартных типов
- `on_serialize_error` — поведение при проблемах сериализации: `"warn"`, `"silent"` или `"raise"`
- `locals_strategy` — стратегия захвата локальных переменных

## Стратегии захвата locals

Поддерживаются две стратегии:

- `settrace` — снимок локальных переменных на `return` или `exception`
- `settrace_full` — обновление снимка на каждой строке; дороже, но полезнее для анализа промежуточных состояний

По умолчанию используется `settrace`.

## Async-функции

Декоратор работает и с `async def`:

```python
from blackbox import blackbox


@blackbox()
async def fetch_user(user_id: int) -> dict:
    payload = {"id": user_id, "name": "Alice"}
    return payload
```

## Выбор сериализатора

По умолчанию библиотека использует `dill`, потому что он лучше справляется
с `lambda`, локальными функциями, closures и другими сложными Python-объектами.

```python
from blackbox import blackbox


@blackbox()
def build_fn():
    scale = 10
    return lambda x: x * scale
```

Если нужен более консервативный режим, можно явно переключиться на `pickle`:

```python
@blackbox(serializer="pickle")
def add(a, b):
    return a + b
```

## Пользовательская санация типов

Если в объектном графе есть нестандартные типы, можно передать свои hook-функции
через `sanitize_hooks`.

В модуле `blackbox.io` доступен sentinel `NOT_HANDLED`, который означает:
hook не знает, как обработать объект, и управление нужно передать дальше.

```python
from blackbox import blackbox
from blackbox.io import NOT_HANDLED


class MyType:
    def __init__(self, value: int):
        self.value = value


def my_hook(obj, context):
    if isinstance(obj, MyType):
        return {
            "type": "MyType",
            "value": obj.value,
            "path": context["path"],
        }
    return NOT_HANDLED


@blackbox(
    sanitize_hooks=[my_hook],
    on_serialize_error="warn",
)
def build_payload():
    return {"custom": MyType(42)}
```

`context` сейчас содержит:

- `path` — путь до объекта внутри снапшота, например `output['result']['custom']`
- `serializer` — активный backend сериализации

Если объект не удалось сериализовать и ни один hook его не обработал, `blackbox`
сохраняет fallback marker вместо падения:

```python
{
    "__blackbox_unserializable__": True,
    "type": "...",
    "module": "...",
    "repr": "...",
    "reason": "...",
}
```

Подробности по таким случаям записываются в:

```python
output_data["__blackbox__"]["serialization_issues"]
```

Эти проблемы не пишутся в `meta.json`, чтобы он оставался легким.

## Загрузка артефактов

В модуле `blackbox.io` есть вспомогательные функции:

```python
from blackbox.io import load_json, load_pickle

meta = load_json("/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34/meta.json")
raw = load_pickle("/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34/output.pkl")
```

Для артефактов, сохраненных через `dill`, можно явно выбрать backend:

```python
raw = load_pickle("/tmp/blackbox/example.py/build_fn/20260425_120102_123456_ab12cd34/output.pkl", serializer="dill")
```

Если снапшот был записан в другом runtime-контексте, например внутри контейнера
или воркера с другим `sys.path`, можно явно передать import roots:

```python
raw = load_pickle(
    "/tmp/blackbox/example.py/build_fn/20260425_120102_123456_ab12cd34/output.pkl",
    serializer="dill",
    import_roots=["/Users/roman/dev/my-project"],
)
```

Для более удобной инспекции есть helper-функции в `blackbox.helpers`:

```python
from blackbox.helpers import get_args, get_local_vars, get_result, read_var_from_pickle

args_info = get_args(
    "/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34/input.pkl",
    import_roots=["/Users/roman/dev/my-project"],
)
locals_info = get_local_vars(
    "/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34/output.pkl",
    import_roots=["/Users/roman/dev/my-project"],
)
result_info = get_result(
    "/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34/output.pkl",
    import_roots=["/Users/roman/dev/my-project"],
)
value_json = read_var_from_pickle(
    "/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34/output.pkl",
    "total",
    import_roots=["/Users/roman/dev/my-project"],
)
```

Что дают `helpers`:

- `get_args(...)` — список входных аргументов с типом, размером и JSON-представлением
- `get_local_vars(...)` — список пойманных локальных переменных с типом и размером
- `get_result(...)` — JSON-представление результата и информация об исключении
- `read_var_from_pickle(...)` — чтение конкретной локальной переменной из `output.pkl`
- `create_snapshot_notebook(...)` — генерация Jupyter notebook для исследования снапшота
- `open_result_in_repl(...)` и `open_var_in_repl(...)` — быстрый экспорт значения в интерактивную сессию
- `import_roots=[...]` — явные корни импорта для снапшотов из другого runtime-контекста

Helper-скрипты также можно запускать напрямую:

```bash
python -m blackbox.helpers.get_result --pickle-path /tmp/blackbox/example.py/add/.../output.pkl --import-root /Users/roman/dev/my-project
python -m blackbox.helpers.get_local_vars --pickle-path /tmp/blackbox/example.py/add/.../output.pkl --import-root /Users/roman/dev/my-project
python -m blackbox.helpers.read_var_from_pickle --pickle-path /tmp/blackbox/example.py/add/.../output.pkl --var-name total --import-root /Users/roman/dev/my-project
python -m blackbox.helpers.create_snapshot_notebook --snapshot-path /tmp/blackbox/example.py/add/.../ --import-root /Users/roman/dev/my-project
python -m blackbox.helpers.create_snapshot_notebook --snapshot-path /tmp/blackbox/example.py/add/.../ --compare-with /tmp/blackbox/example.py/add-other/.../ --import-root /Users/roman/dev/my-project
```

Notebook generation:

```python
from blackbox.helpers import create_snapshot_notebook

notebook_path = create_snapshot_notebook(
    "/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34",
    import_roots=["/Users/roman/dev/my-project"],
)
print(notebook_path)
```

Snapshot comparison notebook:

```python
from blackbox.helpers import create_snapshot_notebook

notebook_path = create_snapshot_notebook(
    "/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34",
    compare_with="/tmp/blackbox/example.py/add/20260425_120301_654321_ef56ab78",
    import_roots=["/Users/roman/dev/my-project"],
)
print(notebook_path)
```

The generated notebook is intended for interactive snapshot review and includes:

- general snapshot information
- loaded `args` and `kwargs`
- loaded local variables
- loaded result
- loaded exception details when present

When `compare_with=...` is provided, the notebook switches to compare mode and includes:

- a high-level summary for both snapshots
- source code comparison
- side-by-side argument inspection
- side-by-side local variable inspection
- side-by-side result inspection
- exception comparison when an exception exists in at least one snapshot

## Ограничения и замечания

- Захват locals через `sys.settrace` добавляет runtime overhead
- `meta.json` содержит только легкие метаданные и не дублирует полные входы/выходы
- `dill` удобнее для сложных объектов, но артефакты сильнее зависят от Python-окружения
- Если объект не pickle-friendly, библиотека старается сохранить его безопасное представление

## Публичное API

```python
from blackbox import blackbox
```

Дополнительные функции чтения артефактов:

```python
from blackbox.io import NOT_HANDLED, load_json, load_pickle
```

Высокоуровневые helper-утилиты:

```python
from blackbox.helpers import (
    create_snapshot_notebook,
    get_args,
    get_local_vars,
    get_result,
    open_result_in_repl,
    open_var_in_repl,
    read_var_from_pickle,
)
```
