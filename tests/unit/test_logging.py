import json
import logging

from gateway.core.logging import JsonFormatter, request_id_var


def format_record(**extra: object) -> dict[str, object]:
    record = logging.LogRecord("gateway.test", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    for key, value in extra.items():
        setattr(record, key, value)
    result: dict[str, object] = json.loads(JsonFormatter().format(record))
    return result


def test_formats_standard_fields_as_json() -> None:
    payload = format_record()

    assert payload["message"] == "hello x"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "gateway.test"
    assert "request_id" not in payload


def test_includes_extra_fields_and_current_request_id() -> None:
    token = request_id_var.set("req-123")
    try:
        payload = format_record(model_alias="fast", duration_ms=12.5)
    finally:
        request_id_var.reset(token)

    assert payload["request_id"] == "req-123"
    assert payload["model_alias"] == "fast"
    assert payload["duration_ms"] == 12.5
    assert "args" not in payload
