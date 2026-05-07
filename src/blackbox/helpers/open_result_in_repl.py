import argparse

try:
    from .common import get_result_data, install_import_guards, load_pickle, parse_import_roots
except ImportError:
    from common import get_result_data, install_import_guards, load_pickle, parse_import_roots


def open_result_in_repl(
    pickle_path: str,
    result_key: str | None = None,
    import_roots: list[str] | None = None,
) -> None:
    install_import_guards()
    data = load_pickle(pickle_path, import_roots=import_roots)
    result_data = get_result_data(data)

    if result_key:
        if not isinstance(result_data, dict):
            raise RuntimeError("RESULT_IS_NOT_A_MAPPING")
        if result_key not in result_data:
            raise KeyError(f"Result key {result_key!r} not found. Available: {list(result_data.keys())}")
        value = result_data[result_key]
        globals()["value"] = value
        globals()[result_key] = value
        print(f"Loaded result key: {result_key}")
        print(f"Available names: value, {result_key}")
        return

    value = result_data
    globals()["value"] = value
    globals()["result"] = value
    print("Loaded result")
    print("Available names: value, result")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pickle-path", required=True)
    parser.add_argument("--result-key")
    parser.add_argument("--import-root", action="append", dest="import_roots")
    args = parser.parse_args()
    open_result_in_repl(
        args.pickle_path,
        args.result_key,
        import_roots=parse_import_roots(args.import_roots),
    )


if __name__ == "__main__":
    main()
