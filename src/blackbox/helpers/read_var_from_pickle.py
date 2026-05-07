import argparse

try:
    from .common import (
        dumps_json,
        get_locals_data,
        install_import_guards,
        load_pickle,
        parse_import_roots,
        serialize_object_view,
    )
except ImportError:
    from common import (
        dumps_json,
        get_locals_data,
        install_import_guards,
        load_pickle,
        parse_import_roots,
        serialize_object_view,
    )


def read_var_from_pickle(
    pickle_path: str,
    var_name: str,
    serialization_mode: str = "auto",
    import_roots: list[str] | None = None,
) -> str:
    install_import_guards()
    data = load_pickle(pickle_path, import_roots=import_roots)
    locals_data = get_locals_data(data)
    if var_name not in locals_data:
        raise KeyError(
            f"Variable {var_name!r} not found. Available: {list(locals_data.keys())}"
        )

    value = locals_data[var_name]
    if serialization_mode == "object":
        return dumps_json(serialize_object_view(value))
    return dumps_json(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pickle-path", required=True)
    parser.add_argument("--var-name", required=True)
    parser.add_argument("--serialization-mode", default="auto")
    parser.add_argument("--import-root", action="append", dest="import_roots")
    args = parser.parse_args()

    print(
        read_var_from_pickle(
            args.pickle_path,
            args.var_name,
            args.serialization_mode,
            import_roots=parse_import_roots(args.import_roots),
        )
    )


if __name__ == "__main__":
    main()
