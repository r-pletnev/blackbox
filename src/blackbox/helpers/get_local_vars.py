import argparse
import json
import sys

try:
    from .common import get_locals_data, install_import_guards, load_pickle, parse_import_roots
except ImportError:
    from common import get_locals_data, install_import_guards, load_pickle, parse_import_roots


def get_local_vars(pickle_path: str, import_roots: list[str] | None = None):
    install_import_guards()
    data = load_pickle(pickle_path, import_roots=import_roots)
    locals_data = get_locals_data(data)
    result = []
    for name, value in locals_data.items():
        result.append(
            {
                "name": name,
                "type": type(value).__name__ if hasattr(type(value), "__name__") else str(type(value)),
                "size": sys.getsizeof(value),
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pickle-path", required=True)
    parser.add_argument("--import-root", action="append", dest="import_roots")
    args = parser.parse_args()
    print(
        json.dumps(
            get_local_vars(args.pickle_path, import_roots=parse_import_roots(args.import_roots)),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
