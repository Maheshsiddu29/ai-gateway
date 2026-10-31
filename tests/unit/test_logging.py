import json
import logging
from collections.abc import Iterator

import pytest

from gateway.core.logging import JsonFormatter, configure_logging, request_id_var


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


@pytest.fixture
def restore_logging() -> Iterator[None]:
    names = ["", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx"]
    saved = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in names
    }
    yield
    for name, (handlers, level, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers, logger.level, logger.propagate = handlers, level, propagate


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_routes_server_logs_to_json_and_drops_duplicates() -> None:
    configure_logging("INFO")

    [handler] = logging.getLogger().handlers
    assert isinstance(handler.formatter, JsonFormatter)
    assert logging.getLogger("uvicorn.error").propagate
    assert not logging.getLogger("uvicorn.error").handlers
    access = logging.getLogger("uvicorn.access")
    assert not access.propagate
    assert not access.handlers
    assert logging.getLogger("httpx").level == logging.WARNING
