import argparse

try:
    from .common import get_locals_data, install_import_guards, load_pickle, parse_import_roots
except ImportError:
    from common import get_locals_data, install_import_guards, load_pickle, parse_import_roots


def open_var_in_repl(
    pickle_path: str,
    var_name: str,
    import_roots: list[str] | None = None,
) -> None:
    install_import_guards()
    data = load_pickle(pickle_path, import_roots=import_roots)
    locals_data = get_locals_data(data)
    if var_name not in locals_data:
        raise KeyError(f"Variable {var_name!r} not found. Available: {list(locals_data.keys())}")

    value = locals_data[var_name]
    globals()["value"] = value
    globals()[var_name] = value
    print(f"Loaded variable: {var_name}")
    print(f"Available names: value, {var_name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pickle-path", required=True)
    parser.add_argument("--var-name", required=True)
    parser.add_argument("--import-root", action="append", dest="import_roots")
    args = parser.parse_args()
    open_var_in_repl(
        args.pickle_path,
        args.var_name,
        import_roots=parse_import_roots(args.import_roots),
    )


if __name__ == "__main__":
    main()
