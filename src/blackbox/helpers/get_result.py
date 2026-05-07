import argparse
import json

try:
    from .common import dumps_json, install_import_guards, load_pickle, parse_import_roots
except ImportError:
    from common import dumps_json, install_import_guards, load_pickle, parse_import_roots


def get_result(pickle_path: str, import_roots: list[str] | None = None):
    install_import_guards()
    data = load_pickle(pickle_path, import_roots=import_roots)
    result_data = data.get("result", {}) if isinstance(data, dict) else {}
    exception = data.get("exception") if isinstance(data, dict) else None
    return {
        "result_json": dumps_json(result_data),
        "exception": str(exception) if exception is not None else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pickle-path", required=True)
    parser.add_argument("--import-root", action="append", dest="import_roots")
    args = parser.parse_args()
    print(
        json.dumps(
            get_result(args.pickle_path, import_roots=parse_import_roots(args.import_roots)),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
