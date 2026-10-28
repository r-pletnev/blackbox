import json
import ssl

from blackbox import blackbox
from blackbox.helpers.common import dumps_json
from blackbox.io import _sanitize_for_pickle, load_pickle


def test_sanitized_mapping_keys_stay_json_encodable():
    payload = {
        ("shape", "S3", 1): ssl.create_default_context(),
        b"bytes key": ssl.create_default_context(),
        "plain": {"nested": 1},
    }

    sanitized = _sanitize_for_pickle(payload, on_serialize_error="silent")

    assert all(isinstance(key, (str, int, float, bool)) for key in sanitized)
    assert json.loads(json.dumps(sanitized))["plain"] == {"nested": 1}


def test_dumps_json_renders_legacy_non_primitive_keys():
    rendered = dumps_json({"s3_client": {"_instance_cache": {("a", 1): "cached"}}})

    cache = json.loads(rendered)["s3_client"]["_instance_cache"]
    assert cache == {"('a', 1)": "cached"}


def test_artifact_with_tuple_keyed_inner_dict_is_readable(tmp_path, monkeypatch):
    monkeypatch.delenv("BLACKBOX_SESSION_ID", raising=False)
    monkeypatch.delenv("BLACKBOX_SESSION_DIR", raising=False)

    @blackbox(save_dir=str(tmp_path), capture_locals=False, on_serialize_error="silent")
    def call():
        return {"client": {"_service_model": {"_instance_cache": {("shape", "S3"): 1}}}}

    call()

    result_path = min(tmp_path.rglob("output.pkl"))
    rendered = dumps_json(load_pickle(str(result_path))["result"])

    assert json.loads(rendered)["client"]["_service_model"]["_instance_cache"] == {
        "('shape', 'S3')": 1
    }
