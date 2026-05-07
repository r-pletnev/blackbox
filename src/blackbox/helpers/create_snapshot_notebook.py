import argparse
import importlib
import inspect
import json
from pathlib import Path
from typing import Any

try:
    from .common import install_import_guards, load_pickle, parse_import_roots
except ImportError:
    from common import install_import_guards, load_pickle, parse_import_roots


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _extract_function_source_from_snapshot(output_data: dict) -> str | None:
    blackbox_meta = (
        output_data.get("__blackbox__", {}) if isinstance(output_data, dict) else {}
    )
    if isinstance(blackbox_meta, dict):
        function_source = blackbox_meta.get("function_source")
        if function_source:
            return function_source
        func = blackbox_meta.get("function")
    else:
        func = None

    if func is not None:
        try:
            dill_source = importlib.import_module("dill.source")
            source = dill_source.getsource(func)
            if source:
                return source
        except Exception:
            pass

        try:
            return inspect.getsource(func)
        except (OSError, TypeError):
            pass

    return None


def _ensure_snapshot_paths(path: str) -> tuple[Path, Path, Path | None, Path]:
    source = Path(path).resolve()
    snapshot_dir = source if source.is_dir() else source.parent

    input_path = snapshot_dir / "input.pkl"
    output_path = snapshot_dir / "output.pkl"
    meta_path = snapshot_dir / "meta.json"

    missing = [str(p.name) for p in (input_path, meta_path) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"Snapshot is incomplete in {snapshot_dir}. Missing: {', '.join(missing)}"
        )

    return snapshot_dir, input_path, output_path if output_path.exists() else None, meta_path


def _format_bytes(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def _markdown_cell(text: str) -> dict:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": text.splitlines(keepends=True),
    }


def _fenced_code_block(code: str, language: str = "python") -> str:
    return "\n".join([f"```{language}", code.rstrip(), "```"])


def _code_cell(code: str, *, hidden: bool = False) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {
            "jupyter": {
                "source_hidden": hidden,
                "outputs_hidden": False,
            }
        },
        "outputs": [],
        "source": code.splitlines(keepends=True),
    }


def _type_name(value: Any) -> str:
    return type(value).__name__ if hasattr(type(value), "__name__") else str(type(value))


def _safe_repr(value: Any, *, limit: int = 200) -> str:
    try:
        text = repr(value)
    except Exception as exc:
        text = f"<repr failed: {exc}>"
    return text if len(text) <= limit else f"{text[:limit - 3]}..."


def _snapshot_label(bundle: dict) -> str:
    return bundle["meta"].get("title") or bundle["snapshot_dir"].name


def _has_output(bundle: dict) -> bool:
    return bundle["output_path"] is not None and isinstance(bundle["output_data"], dict)


def _load_snapshot_bundle(path: str, import_roots: list[str] | None = None) -> dict:
    snapshot_dir, input_path, output_path, meta_path = _ensure_snapshot_paths(path)
    meta = _read_json(meta_path)
    serializer = meta.get("serializer", "auto")
    input_data = load_pickle(
        str(input_path),
        serializer=serializer,
        import_roots=import_roots,
    )
    output_data = (
        load_pickle(
            str(output_path),
            serializer=serializer,
            import_roots=import_roots,
        )
        if output_path
        else {}
    )
    return {
        "snapshot_dir": snapshot_dir,
        "input_path": input_path,
        "output_path": output_path,
        "meta_path": meta_path,
        "meta": meta,
        "serializer": serializer,
        "import_roots": import_roots or [],
        "input_data": input_data,
        "output_data": output_data,
        "has_output": output_path is not None,
        "source_code": _extract_function_source_from_snapshot(output_data),
        "pickle_size": output_path.stat().st_size if output_path else None,
    }


def _build_args_description(input_data: dict) -> str:
    args = input_data.get("args", [])
    kwargs = input_data.get("kwargs", {})
    lines = []

    if args:
        lines.append("Positional arguments:")
        for index, value in enumerate(args):
            lines.append(f"- `args[{index}]`: `{_type_name(value)}`")
    else:
        lines.append("Positional arguments: none")

    if kwargs:
        lines.append("")
        lines.append("Keyword arguments:")
        for name, value in kwargs.items():
            lines.append(f"- `{name}`: `{_type_name(value)}`")
    else:
        lines.append("")
        lines.append("Keyword arguments: none")

    return "\n".join(lines)


def _build_locals_description(output_data: dict) -> str:
    if not isinstance(output_data, dict):
        return "Output artifact is unavailable for this snapshot."
    locals_data = output_data.get("locals", {})
    if not locals_data:
        return "No local variables were captured in this snapshot."

    lines = ["Captured local variables:"]
    for name, value in locals_data.items():
        lines.append(f"- `{name}`: `{_type_name(value)}`")
    return "\n".join(lines)


def _build_result_description(output_data: dict) -> str:
    if not isinstance(output_data, dict):
        return "Output artifact is unavailable for this snapshot."
    result = output_data.get("result")
    return "\n".join(
        [
            "Captured result:",
            f"- Type: `{_type_name(result)}`",
            f"- Preview: `{_safe_repr(result)}`",
        ]
    )


def _build_exception_description(output_data: dict) -> str:
    if not isinstance(output_data, dict):
        return "Output artifact is unavailable for this snapshot."
    exception = output_data.get("exception")
    if not exception:
        return "No exception was captured for this snapshot."

    return "\n".join(
        [
            "Captured exception:",
            f"- Type: `{exception.get('type', 'Unknown')}`",
            f"- Message: `{exception.get('message', '')}`",
            "",
            "Traceback:",
            "```text",
            exception.get("traceback", "").rstrip(),
            "```",
        ]
    )


def _build_general_information(bundle: dict) -> str:
    meta = bundle["meta"]
    snapshot_name = _snapshot_label(bundle)
    return "\n".join(
        [
            "## General Information",
            "",
            f"- Function name: `{meta.get('function', 'unknown')}`",
            f"- Async function: `{'yes' if meta.get('isAsync') else 'no'}`",
            f"- Source file path: `{meta.get('sourcePath', '')}`",
            f"- Snapshot name: `{snapshot_name}`",
            f"- Git branch: `{meta.get('gitBranch')}`"
            if meta.get("gitBranch")
            else "- Git branch: unavailable",
            f"- Snapshot creation time: `{meta.get('created')}`"
            if meta.get("created")
            else "- Snapshot creation time: unavailable",
            f"- Elapsed time: `{meta.get('elapsedMs')} ms`"
            if meta.get("elapsedMs") is not None
            else "- Elapsed time: unavailable",
            f"- Serialized output size: `{_format_bytes(bundle['pickle_size'])}`"
            if bundle["pickle_size"] is not None
            else "- Serialized output size: unavailable (output.pkl is missing)",
            f"- Serializer: `{bundle['serializer']}`",
            f"- Output artifact available: `{'yes' if bundle['has_output'] else 'no'}`",
        ]
    )


def _build_source_code_block(bundle: dict) -> str:
    source_path = bundle["meta"].get("sourcePath", "")
    source_code = bundle["source_code"]
    return "\n".join(
        [
            "## Source Code",
            "",
            _fenced_code_block(source_code)
            if source_code
            else (
                "Source code could not be extracted from the serialized function "
                f"object. See the recorded source file: `{source_path}`"
            ),
        ]
    )


def _build_bootstrap_code_single(bundle: dict) -> str:
    import_roots_line = f"import_roots = {bundle['import_roots']!r}"
    output_line = (
        f"output_data = load_pickle({str(bundle['output_path'])!r}, serializer={bundle['serializer']!r}, import_roots=import_roots)"
        if bundle["output_path"] is not None
        else "output_data = {}"
    )
    return "\n".join(
        [
            "from pprint import pprint",
            "from blackbox.io import load_pickle",
            "",
            import_roots_line,
            f"input_data = load_pickle({str(bundle['input_path'])!r}, serializer={bundle['serializer']!r}, import_roots=import_roots)",
            output_line,
            "args = input_data.get('args', ())",
            "kwargs = input_data.get('kwargs', {})",
            "locals_data = output_data.get('locals', {})",
            "result = output_data.get('result')",
            "exception = output_data.get('exception')",
            "",
            "for _index, _value in enumerate(args):",
            "    globals()[f'arg_{_index}'] = _value",
            "for _key, _value in kwargs.items():",
            "    globals()[_key] = _value",
            "for _name, _value in locals_data.items():",
            "    globals()[_name] = _value",
        ]
    )


def _common_key_set(left: dict, right: dict) -> tuple[list[str], list[str], list[str]]:
    left_keys = set(left.keys())
    right_keys = set(right.keys())
    only_left = sorted(left_keys - right_keys)
    only_right = sorted(right_keys - left_keys)
    common = sorted(left_keys & right_keys)
    return only_left, only_right, common


def _build_compare_summary(bundle_a: dict, bundle_b: dict) -> str:
    meta_a = bundle_a["meta"]
    meta_b = bundle_b["meta"]
    input_a = bundle_a["input_data"]
    input_b = bundle_b["input_data"]
    output_a = bundle_a["output_data"]
    output_b = bundle_b["output_data"]

    kwargs_a = input_a.get("kwargs", {}) if isinstance(input_a, dict) else {}
    kwargs_b = input_b.get("kwargs", {}) if isinstance(input_b, dict) else {}
    locals_a = output_a.get("locals", {}) if isinstance(output_a, dict) else {}
    locals_b = output_b.get("locals", {}) if isinstance(output_b, dict) else {}
    _, _, common_kwargs = _common_key_set(kwargs_a, kwargs_b)
    _, _, common_locals = _common_key_set(locals_a, locals_b)

    return "\n".join(
        [
            "## General Information",
            "",
            f"- Snapshot A: `{_snapshot_label(bundle_a)}`",
            f"- Snapshot B: `{_snapshot_label(bundle_b)}`",
            f"- Same function name: `{'yes' if meta_a.get('function') == meta_b.get('function') else 'no'}`",
            f"- Same source file: `{'yes' if meta_a.get('sourcePath') == meta_b.get('sourcePath') else 'no'}`",
            f"- Same git branch: `{'yes' if meta_a.get('gitBranch') == meta_b.get('gitBranch') else 'no'}`",
            f"- Positional args count: `{len(input_a.get('args', ()))}` vs `{len(input_b.get('args', ()))}`",
            f"- Shared kwarg names: `{len(common_kwargs)}`",
            f"- Shared local variable names: `{len(common_locals)}`",
            f"- Result type: `{_type_name(output_a.get('result'))}` vs `{_type_name(output_b.get('result'))}`",
            f"- Exception present: `{'yes' if output_a.get('exception') else 'no'}` vs `{'yes' if output_b.get('exception') else 'no'}`",
            f"- Elapsed time: `{meta_a.get('elapsedMs', 'n/a')} ms` vs `{meta_b.get('elapsedMs', 'n/a')} ms`",
            f"- Output artifact available: `{'yes' if bundle_a['has_output'] else 'no'}` vs `{'yes' if bundle_b['has_output'] else 'no'}`",
            f"- Output size: "
            f"`{_format_bytes(bundle_a['pickle_size']) if bundle_a['pickle_size'] is not None else 'n/a'}` vs "
            f"`{_format_bytes(bundle_b['pickle_size']) if bundle_b['pickle_size'] is not None else 'n/a'}`",
        ]
    )


def _build_compare_source_code_block(bundle_a: dict, bundle_b: dict) -> str:
    source_a = bundle_a["source_code"]
    source_b = bundle_b["source_code"]
    path_a = bundle_a["meta"].get("sourcePath", "")
    path_b = bundle_b["meta"].get("sourcePath", "")

    if source_a and source_b and source_a == source_b:
        body = _fenced_code_block(source_a)
    elif source_a and source_b:
        body = "\n".join(
            [
                "Source code differs between snapshots.",
                "",
                "Run the diff cell below to render an HTML diff in JupyterLab.",
            ]
        )
    else:
        parts = []
        parts.append(f"### Snapshot A: `{_snapshot_label(bundle_a)}`")
        parts.append(
            _fenced_code_block(source_a)
            if source_a
            else (
                "Source code could not be extracted from the serialized function "
                f"object. See the recorded source file: `{path_a}`"
            )
        )
        parts.append("")
        parts.append(f"### Snapshot B: `{_snapshot_label(bundle_b)}`")
        parts.append(
            _fenced_code_block(source_b)
            if source_b
            else (
                "Source code could not be extracted from the serialized function "
                f"object. See the recorded source file: `{path_b}`"
            )
        )
        body = "\n".join(parts)

    return "\n".join(["## Source Code", "", body])


def _build_bootstrap_code_compare(bundle_a: dict, bundle_b: dict) -> str:
    import_roots_line = f"import_roots = {bundle_a['import_roots']!r}"
    output_a_line = (
        f"output_data_a = load_pickle({str(bundle_a['output_path'])!r}, serializer={bundle_a['serializer']!r}, import_roots=import_roots)"
        if bundle_a["output_path"] is not None
        else "output_data_a = {}"
    )
    output_b_line = (
        f"output_data_b = load_pickle({str(bundle_b['output_path'])!r}, serializer={bundle_b['serializer']!r}, import_roots=import_roots)"
        if bundle_b["output_path"] is not None
        else "output_data_b = {}"
    )
    return "\n".join(
        [
            "import difflib",
            "from pprint import pprint",
            "from IPython.display import HTML, display",
            "from blackbox.io import load_pickle",
            "",
            import_roots_line,
            "",
            "def show_html_diff(",
            "    old: str,",
            "    new: str,",
            "    old_name: str = 'old',",
            "    new_name: str = 'new',",
            "    context: bool = True,",
            "    numlines: int = 3,",
            "    wrapcolumn: int | None = None,",
            ") -> None:",
            "    old_lines = old.splitlines()",
            "    new_lines = new.splitlines()",
            "",
            "    differ = difflib.HtmlDiff(",
            "        tabsize=4,",
            "        wrapcolumn=wrapcolumn,",
            "    )",
            "",
            "    html_table = differ.make_table(",
            "        old_lines,",
            "        new_lines,",
            "        fromdesc=old_name,",
            "        todesc=new_name,",
            "        context=context,",
            "        numlines=numlines,",
            "    )",
            "",
            "    style = '''",
            "    <style>",
            "        .diff-wrapper {",
            "            width: 100%;",
            "            max-width: 100%;",
            "            overflow-x: auto;",
            "            overflow-y: hidden;",
            "            border: 1px solid #3a3a3a;",
            "            border-radius: 8px;",
            "            background: #1e1e1e;",
            "            padding: 0;",
            "        }",
            "",
            "        .diff-wrapper table.diff {",
            "            border-collapse: collapse;",
            "            table-layout: auto;",
            "            width: max-content;",
            "            min-width: 100%;",
            "            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", monospace;",
            "            font-size: 13px;",
            "            line-height: 1.45;",
            "            background: #1e1e1e;",
            "            color: #d4d4d4;",
            "        }",
            "",
            "        .diff-wrapper table.diff th,",
            "        .diff-wrapper table.diff td {",
            "            padding: 2px 8px;",
            "            border: none;",
            "            white-space: pre;",
            "            vertical-align: top;",
            "        }",
            "",
            "        .diff-wrapper .diff_header {",
            "            background: #263238;",
            "            color: #79b8ff;",
            "            font-weight: 600;",
            "            text-align: right;",
            "            user-select: none;",
            "        }",
            "",
            "        .diff-wrapper th.diff_header {",
            "            text-align: center;",
            "        }",
            "",
            "        .diff-wrapper .diff_next {",
            "            display: none;",
            "        }",
            "",
            "        .diff-wrapper td:nth-child(2),",
            "        .diff-wrapper td:nth-child(5) {",
            "            min-width: 42px;",
            "            color: #79b8ff;",
            "            background: #263238;",
            "            text-align: right;",
            "            user-select: none;",
            "        }",
            "",
            "        .diff-wrapper td:nth-child(3),",
            "        .diff-wrapper td:nth-child(6) {",
            "            min-width: 600px;",
            "            background: #f7f7f7;",
            "            color: #111111;",
            "        }",
            "",
            "        .diff-wrapper .diff_add {",
            "            background-color: #1f6f3a;",
            "            color: #ffffff;",
            "        }",
            "",
            "        .diff-wrapper .diff_sub {",
            "            background-color: #9b2c2c;",
            "            color: #ffffff;",
            "        }",
            "",
            "        .diff-wrapper .diff_chg {",
            "            background-color: #8a6d1d;",
            "            color: #ffffff;",
            "        }",
            "",
            "        .diff-wrapper tbody tr:nth-child(even) td:nth-child(3),",
            "        .diff-wrapper tbody tr:nth-child(even) td:nth-child(6) {",
            "            background: #eeeeee;",
            "        }",
            "",
            "        .diff-wrapper tbody tr:nth-child(odd) td:nth-child(3),",
            "        .diff-wrapper tbody tr:nth-child(odd) td:nth-child(6) {",
            "            background: #f8f8f8;",
            "        }",
            "    </style>",
            "    '''",
            "",
            "    display(HTML(style + f'<div class=\"diff-wrapper\">{html_table}</div>'))",
            "",
            f"input_data_a = load_pickle({str(bundle_a['input_path'])!r}, serializer={bundle_a['serializer']!r}, import_roots=import_roots)",
            output_a_line,
            f"input_data_b = load_pickle({str(bundle_b['input_path'])!r}, serializer={bundle_b['serializer']!r}, import_roots=import_roots)",
            output_b_line,
            "",
            "args_a = input_data_a.get('args', ())",
            "kwargs_a = input_data_a.get('kwargs', {})",
            "locals_data_a = output_data_a.get('locals', {})",
            "result_a = output_data_a.get('result')",
            "exception_a = output_data_a.get('exception')",
            "",
            "args_b = input_data_b.get('args', ())",
            "kwargs_b = input_data_b.get('kwargs', {})",
            "locals_data_b = output_data_b.get('locals', {})",
            "result_b = output_data_b.get('result')",
            "exception_b = output_data_b.get('exception')",
            f"source_code_a = {bundle_a['source_code']!r}",
            f"source_code_b = {bundle_b['source_code']!r}",
            "",
            "for _index, _value in enumerate(args_a):",
            "    globals()[f'arg_a_{_index}'] = _value",
            "for _key, _value in kwargs_a.items():",
            "    globals()[f'kwarg_a_{_key}'] = _value",
            "for _name, _value in locals_data_a.items():",
            "    globals()[f'local_a_{_name}'] = _value",
            "",
            "for _index, _value in enumerate(args_b):",
            "    globals()[f'arg_b_{_index}'] = _value",
            "for _key, _value in kwargs_b.items():",
            "    globals()[f'kwarg_b_{_key}'] = _value",
            "for _name, _value in locals_data_b.items():",
            "    globals()[f'local_b_{_name}'] = _value",
        ]
    )


def _build_args_compare_summary(bundle_a: dict, bundle_b: dict) -> str:
    input_a = bundle_a["input_data"]
    input_b = bundle_b["input_data"]
    args_a = input_a.get("args", ()) if isinstance(input_a, dict) else ()
    args_b = input_b.get("args", ()) if isinstance(input_b, dict) else ()
    kwargs_a = input_a.get("kwargs", {}) if isinstance(input_a, dict) else {}
    kwargs_b = input_b.get("kwargs", {}) if isinstance(input_b, dict) else {}
    only_a, only_b, common = _common_key_set(kwargs_a, kwargs_b)

    return "\n".join(
        [
            "## Arguments",
            "",
            f"- Snapshot A positional args: `{len(args_a)}`",
            f"- Snapshot B positional args: `{len(args_b)}`",
            f"- Snapshot A kwargs: `{sorted(kwargs_a.keys())}`",
            f"- Snapshot B kwargs: `{sorted(kwargs_b.keys())}`",
            f"- Kwargs only in A: `{only_a}`",
            f"- Kwargs only in B: `{only_b}`",
            f"- Shared kwargs: `{common}`",
        ]
    )


def _build_locals_compare_summary(bundle_a: dict, bundle_b: dict) -> str:
    locals_a = bundle_a["output_data"].get("locals", {})
    locals_b = bundle_b["output_data"].get("locals", {})
    only_a, only_b, common = _common_key_set(locals_a, locals_b)

    changed_type = [
        name
        for name in common
        if _type_name(locals_a[name]) != _type_name(locals_b[name])
    ]

    return "\n".join(
        [
            "## Local Variables",
            "",
            f"- Locals only in A: `{only_a}`",
            f"- Locals only in B: `{only_b}`",
            f"- Shared local names: `{common}`",
            f"- Shared locals with different type: `{changed_type}`",
        ]
    )


def _build_result_compare_summary(bundle_a: dict, bundle_b: dict) -> str:
    result_a = bundle_a["output_data"].get("result")
    result_b = bundle_b["output_data"].get("result")
    return "\n".join(
        [
            "## Result",
            "",
            f"- Snapshot A result type: `{_type_name(result_a)}`",
            f"- Snapshot B result type: `{_type_name(result_b)}`",
            f"- Same result type: `{'yes' if _type_name(result_a) == _type_name(result_b) else 'no'}`",
            f"- Snapshot A preview: `{_safe_repr(result_a)}`",
            f"- Snapshot B preview: `{_safe_repr(result_b)}`",
        ]
    )


def _build_exception_compare_summary(bundle_a: dict, bundle_b: dict) -> str:
    exc_a = bundle_a["output_data"].get("exception")
    exc_b = bundle_b["output_data"].get("exception")
    return "\n".join(
        [
            "## Exception",
            "",
            f"- Snapshot A exception: `{exc_a.get('type') if exc_a else 'none'}`",
            f"- Snapshot B exception: `{exc_b.get('type') if exc_b else 'none'}`",
        ]
    )


def _build_single_snapshot_notebook(bundle: dict) -> dict:
    cells = [
        _markdown_cell(
            "# Snapshot Review Notebook\n\nThis notebook was generated from a blackbox snapshot."
        ),
        _markdown_cell(
            "## How To Use This Notebook\n\n"
            "This notebook includes a hidden initialization cell.\n"
            "In JupyterLab, run that hidden cell first before using the review cells below.\n\n"
            "After the initialization cell has run, the visible cells will display the loaded snapshot data."
        ),
        _markdown_cell(_build_general_information(bundle)),
        _markdown_cell(_build_source_code_block(bundle)),
        _code_cell(_build_bootstrap_code_single(bundle), hidden=True),
        _markdown_cell("## Arguments\n\n" + _build_args_description(bundle["input_data"])),
        _code_cell("pprint(input_data)"),
        _markdown_cell(
            "## Local Variables\n\n"
            + _build_locals_description(bundle["output_data"])
        ),
        _code_cell("pprint(locals_data)"),
        _markdown_cell("## Result\n\n" + _build_result_description(bundle["output_data"])),
        _code_cell("pprint(result)"),
    ]

    if isinstance(bundle["output_data"], dict) and bundle["output_data"].get("exception"):
        cells.extend(
            [
                _markdown_cell(
                    "## Exception\n\n"
                    + _build_exception_description(bundle["output_data"])
                ),
                _code_cell("pprint(exception)"),
            ]
        )

    return cells


def _build_compare_snapshot_notebook(bundle_a: dict, bundle_b: dict) -> list[dict]:
    cells = [
        _markdown_cell(
            "# Snapshot Compare Notebook\n\nThis notebook was generated from two blackbox snapshots."
        ),
        _markdown_cell(
            "## How To Use This Notebook\n\n"
            "This notebook includes a hidden initialization cell.\n"
            "In JupyterLab, run that hidden cell first before using the review cells below.\n\n"
            "After the initialization cell has run, the visible cells will display the loaded snapshot data for both snapshots."
        ),
        _markdown_cell(_build_compare_summary(bundle_a, bundle_b)),
        _markdown_cell(_build_compare_source_code_block(bundle_a, bundle_b)),
        _code_cell(_build_bootstrap_code_compare(bundle_a, bundle_b), hidden=True),
        _code_cell(
            "\n".join(
                [
                    "if source_code_a and source_code_b and source_code_a != source_code_b:",
                    "    show_html_diff(",
                    "        source_code_a,",
                    "        source_code_b,",
                    f"        old_name={_snapshot_label(bundle_a)!r},",
                    f"        new_name={_snapshot_label(bundle_b)!r},",
                    "    )",
                    "elif source_code_a and source_code_b:",
                    "    print('Source code is identical across both snapshots.')",
                    "else:",
                    "    print('Source diff is unavailable because source extraction failed for at least one snapshot.')",
                ]
            )
        ),
        _markdown_cell(_build_args_compare_summary(bundle_a, bundle_b)),
        _markdown_cell(f"### Snapshot A: `{_snapshot_label(bundle_a)}`"),
        _code_cell("pprint(input_data_a)"),
        _markdown_cell(f"### Snapshot B: `{_snapshot_label(bundle_b)}`"),
        _code_cell("pprint(input_data_b)"),
        _markdown_cell(_build_locals_compare_summary(bundle_a, bundle_b)),
        _markdown_cell(f"### Snapshot A: `{_snapshot_label(bundle_a)}`"),
        _code_cell("pprint(locals_data_a)"),
        _markdown_cell(f"### Snapshot B: `{_snapshot_label(bundle_b)}`"),
        _code_cell("pprint(locals_data_b)"),
        _markdown_cell(_build_result_compare_summary(bundle_a, bundle_b)),
        _markdown_cell(f"### Snapshot A: `{_snapshot_label(bundle_a)}`"),
        _code_cell("pprint(result_a)"),
        _markdown_cell(f"### Snapshot B: `{_snapshot_label(bundle_b)}`"),
        _code_cell("pprint(result_b)"),
    ]

    if (
        isinstance(bundle_a["output_data"], dict)
        and bundle_a["output_data"].get("exception")
    ) or (
        isinstance(bundle_b["output_data"], dict)
        and bundle_b["output_data"].get("exception")
    ):
        cells.extend(
            [
                _markdown_cell(_build_exception_compare_summary(bundle_a, bundle_b)),
                _markdown_cell(f"### Snapshot A: `{_snapshot_label(bundle_a)}`"),
                _code_cell("pprint(exception_a)"),
                _markdown_cell(f"### Snapshot B: `{_snapshot_label(bundle_b)}`"),
                _code_cell("pprint(exception_b)"),
            ]
        )

    return cells


def create_snapshot_notebook(
    snapshot_path: str,
    compare_with: str | None = None,
    output_path: str | None = None,
    import_roots: list[str] | None = None,
) -> str:
    install_import_guards()

    bundle = _load_snapshot_bundle(snapshot_path, import_roots=import_roots)
    compare_bundle = (
        _load_snapshot_bundle(compare_with, import_roots=import_roots) if compare_with else None
    )

    if compare_bundle is None:
        notebook_path = (
            Path(output_path).resolve()
            if output_path
            else bundle["snapshot_dir"] / "snapshot_review.ipynb"
        )
        cells = _build_single_snapshot_notebook(bundle)
    else:
        compare_name = compare_bundle["snapshot_dir"].name
        notebook_path = (
            Path(output_path).resolve()
            if output_path
            else bundle["snapshot_dir"] / f"snapshot_compare__{compare_name}.ipynb"
        )
        cells = _build_compare_snapshot_notebook(bundle, compare_bundle)

    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {
                "name": "python",
            },
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }

    with notebook_path.open("w", encoding="utf-8") as f:
        json.dump(notebook, f, ensure_ascii=False, indent=2)

    return str(notebook_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-path", required=True)
    parser.add_argument("--compare-with")
    parser.add_argument("--output-path")
    parser.add_argument("--import-root", action="append", dest="import_roots")
    args = parser.parse_args()
    print(
        create_snapshot_notebook(
            args.snapshot_path,
            compare_with=args.compare_with,
            output_path=args.output_path,
            import_roots=parse_import_roots(args.import_roots),
        )
    )


if __name__ == "__main__":
    main()
