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

Для более удобной инспекции есть helper-функции в `blackbox.helpers`:

```python
from blackbox.helpers import get_args, get_local_vars, get_result, read_var_from_pickle

args_info = get_args("/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34/input.pkl")
locals_info = get_local_vars("/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34/output.pkl")
result_info = get_result("/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34/output.pkl")
value_json = read_var_from_pickle(
    "/tmp/blackbox/example.py/add/20260425_120102_123456_ab12cd34/output.pkl",
    "total",
)
```

Что дают `helpers`:

- `get_args(...)` — список входных аргументов с типом, размером и JSON-представлением
- `get_local_vars(...)` — список пойманных локальных переменных с типом и размером
- `get_result(...)` — JSON-представление результата и информация об исключении
- `read_var_from_pickle(...)` — чтение конкретной локальной переменной из `output.pkl`
- `open_result_in_repl(...)` и `open_var_in_repl(...)` — быстрый экспорт значения в интерактивную сессию

Helper-скрипты также можно запускать напрямую:

```bash
python -m blackbox.helpers.get_result --pickle-path /tmp/blackbox/example.py/add/.../output.pkl
python -m blackbox.helpers.get_local_vars --pickle-path /tmp/blackbox/example.py/add/.../output.pkl
python -m blackbox.helpers.read_var_from_pickle --pickle-path /tmp/blackbox/example.py/add/.../output.pkl --var-name total
```

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
from blackbox.io import load_json, load_pickle
```

Высокоуровневые helper-утилиты:

```python
from blackbox.helpers import (
    get_args,
    get_local_vars,
    get_result,
    open_result_in_repl,
    open_var_in_repl,
    read_var_from_pickle,
)
```
