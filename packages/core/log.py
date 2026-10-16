"""
Structured logging -- one configuration point, JSON in production.

Every log line is a single JSON object (or a readable console line in dev) that
automatically carries whatever identifying context is bound at that moment
(company_id, product_id, agent_id, task_id, workflow_id, request_id, worker_id)
via `bind_log_context`, so a log line can always be joined back to the
tasks / activity_events / audit_log rows it describes.

Secrets never reach a log line: field names that look sensitive are replaced,
and bearer tokens / credentials embedded in URLs are scrubbed from message text.
Redaction is a safety net -- the rule is still "don't log secrets" -- but it is
tested, so a mistake degrades to `[REDACTED]`, not to a leaked credential.

Named `log.py` (not `logging.py`) so it can never shadow the stdlib module.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

REDACTED = "[REDACTED]"
ROOT_LOGGER_NAME = "sparserest"

_SENSITIVE_KEY = re.compile(r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|cookie|credential|private[_-]?key)", re.I)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=\-]+", re.I)
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s]+(@)", re.I)

_log_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("sparserest_log_context", default={})

_STANDARD_RECORD_ATTRS = set(logging.LogRecord("x", 0, "x", 0, "", (), None).__dict__) | {"message", "asctime", "fields"}


def redact(value: Any, key: Optional[str] = None) -> Any:
    """Scrub a value headed for a log line. Sensitive-looking keys are replaced
    outright; strings have bearer tokens and URL-embedded credentials removed."""
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return _URL_CREDENTIALS.sub(r"\1" + REDACTED + r"\2", _BEARER.sub(r"\1" + REDACTED, value))
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(v) for v in value]
    return value


@contextmanager
def bind_log_context(**fields: Any) -> Iterator[None]:
    """Attach fields to every log line emitted inside the `with` block (including
    from deeper calls). None values are dropped; UUIDs are stringified."""
    merged = dict(_log_context.get())
    merged.update({k: str(v) for k, v in fields.items() if v is not None})
    token = _log_context.set(merged)
    try:
        yield
    finally:
        _log_context.reset(token)


def current_log_context() -> dict[str, Any]:
    return dict(_log_context.get())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        payload.update(redact(current_log_context()))
        payload.update(redact(getattr(record, "fields", {}) or {}))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = {**current_log_context(), **(getattr(record, "fields", {}) or {})}
        suffix = " ".join(f"{k}={v}" for k, v in redact(fields).items())
        line = f"{datetime.fromtimestamp(record.created).strftime('%H:%M:%S')} {record.levelname:<7} {record.name} | {redact(record.getMessage())}"
        if suffix:
            line += f" | {suffix}"
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def configure_logging(service: str = "api", *, level: Optional[str] = None, fmt: Optional[str] = None, stream=None) -> None:
    """Idempotent -- safe to call from every entrypoint (API, worker, inbound
    checker, scripts). Replaces only the handler this function installed."""
    from packages.core.config import get_settings
    settings = get_settings()

    level_name = (level or settings.log_level or "INFO").upper()
    chosen = (fmt or settings.log_format or "auto").lower()
    if chosen == "auto":
        chosen = "json" if settings.is_production else "console"

    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        if getattr(handler, "_sparserest_handler", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stdout)
    handler._sparserest_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter() if chosen == "json" else ConsoleFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name, logging.INFO))
    root.propagate = False

    # Uvicorn's own access log would duplicate (unstructured) what the API's
    # request-logging middleware already records.
    logging.getLogger("uvicorn.access").disabled = True

    root.info("logging configured", extra={"fields": {"service": service, "format": chosen}})


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def log(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """logger.log with structured fields: log(logger, logging.INFO, "task done", duration_ms=12)."""
    logger.log(level, message, extra={"fields": fields})
