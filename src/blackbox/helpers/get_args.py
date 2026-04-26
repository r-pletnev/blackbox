import argparse
import json
import sys

try:
    from .common import dumps_json, install_import_guards, load_pickle
except ImportError:
    from common import dumps_json, install_import_guards, load_pickle


def get_args(input_path: str):
    install_import_guards()
    data = load_pickle(input_path)
    args_data = data if isinstance(data, dict) else {}
    args_list = args_data.get("args", [])
    kwargs_data = args_data.get("kwargs", {})

    result = []
    for index, value in enumerate(args_list):
        result.append(
            {
                "name": f"args[{index}]",
                "type": type(value).__name__ if hasattr(type(value), "__name__") else str(type(value)),
                "size": sys.getsizeof(value),
                "json": dumps_json(value),
            }
        )
    for key, value in kwargs_data.items():
        result.append(
            {
                "name": key,
                "type": type(value).__name__ if hasattr(type(value), "__name__") else str(type(value)),
                "size": sys.getsizeof(value),
                "json": dumps_json(value),
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-path", required=True)
    args = parser.parse_args()
    print(json.dumps(get_args(args.input_path), ensure_ascii=False))


if __name__ == "__main__":
    main()
