"""JSON logging on top of the standard library.

Fields passed through ``extra=`` become top-level JSON keys, and the current
request ID is attached from a context variable. Callers must never pass
credentials, incident text, prompts, or model output as log fields.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("agent_request_id", default=None)

_STANDARD_RECORD_FIELDS = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))
) | {"message", "asctime", "color_message"}

# httpx logs every request URL at INFO.
_QUIET_LOGGERS = ("httpx", "httpcore")
_SERVER_LOGGERS = ("uvicorn", "uvicorn.error")
# uvicorn's access log duplicates the runtime's own and includes query strings.
_DISABLED_LOGGERS = ("uvicorn.access",)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in _SERVER_LOGGERS:
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in _DISABLED_LOGGERS:
        disabled_logger = logging.getLogger(name)
        disabled_logger.handlers.clear()
        disabled_logger.propagate = False
