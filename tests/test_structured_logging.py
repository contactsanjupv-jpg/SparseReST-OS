"""Structured logging: JSON shape, bound context, and -- most importantly --
that secrets cannot reach a log line."""
from __future__ import annotations

import io
import json
import logging
import uuid

from packages.core.log import (
    REDACTED, bind_log_context, configure_logging, current_log_context, get_logger, log, redact,
)


def _capture(fmt: str = "json") -> io.StringIO:
    stream = io.StringIO()
    configure_logging("test", level="DEBUG", fmt=fmt, stream=stream)
    return stream


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(l) for l in stream.getvalue().splitlines() if l.strip()]


def test_json_lines_are_valid_and_carry_fields():
    stream = _capture()
    log(get_logger("unit"), logging.INFO, "task finished", duration_ms=42, agent="sales")
    record = _lines(stream)[-1]
    assert record["message"] == "task finished"
    assert record["level"] == "INFO"
    assert record["logger"] == "sparserest.unit"
    assert record["duration_ms"] == 42 and record["agent"] == "sales"
    assert record["ts"].endswith("+00:00")


def test_bound_context_is_attached_and_released():
    stream = _capture()
    task_id = uuid.uuid4()
    with bind_log_context(task_id=task_id, product_id=None, worker_id="w1"):
        log(get_logger("unit"), logging.INFO, "inside")
        assert current_log_context()["task_id"] == str(task_id)
    log(get_logger("unit"), logging.INFO, "outside")
    inside, outside = _lines(stream)[-2:]
    assert inside["task_id"] == str(task_id) and inside["worker_id"] == "w1"
    assert "product_id" not in inside              # None values are dropped, not logged as "None"
    assert "task_id" not in outside


def test_nested_context_merges_and_unwinds():
    with bind_log_context(a=1):
        with bind_log_context(b=2):
            assert current_log_context() == {"a": "1", "b": "2"}
        assert current_log_context() == {"a": "1"}
    assert current_log_context() == {}


def test_sensitive_fields_are_redacted():
    stream = _capture()
    log(get_logger("unit"), logging.INFO, "config loaded", smtp_password="hunter2", api_key="sk-abc",
        nested={"Authorization": "Bearer abc.def", "ok": "visible"}, founder_token="t0k")
    record = _lines(stream)[-1]
    assert record["smtp_password"] == REDACTED
    assert record["api_key"] == REDACTED
    assert record["founder_token"] == REDACTED
    assert record["nested"]["Authorization"] == REDACTED
    assert record["nested"]["ok"] == "visible"
    assert "hunter2" not in stream.getvalue() and "sk-abc" not in stream.getvalue()


def test_secrets_embedded_in_message_text_are_scrubbed():
    stream = _capture()
    get_logger("unit").info("calling with Bearer abc123.def-456 to postgresql://app_role:s3cr3t@db:5432/x")
    text = stream.getvalue()
    assert "abc123.def-456" not in text and "s3cr3t" not in text
    assert "postgresql://app_role:" in text and "@db:5432/x" in text   # still useful for debugging


def test_exception_tracebacks_are_redacted():
    stream = _capture()
    try:
        raise RuntimeError("failed auth with Bearer topsecret-token")
    except RuntimeError:
        get_logger("unit").exception("boom")
    record = _lines(stream)[-1]
    assert "topsecret-token" not in record["exc"]


def test_console_format_is_human_readable_and_redacted():
    stream = _capture(fmt="console")
    with bind_log_context(task_id="t-1"):
        log(get_logger("unit"), logging.WARNING, "slow task", password="x", seconds=9)
    out = stream.getvalue().splitlines()[-1]
    assert "WARNING" in out and "slow task" in out and "task_id=t-1" in out and "seconds=9" in out
    assert "password=[REDACTED]" in out


def test_configure_is_idempotent_no_duplicate_lines():
    stream = _capture()
    configure_logging("test", level="DEBUG", fmt="json", stream=stream)
    configure_logging("test", level="DEBUG", fmt="json", stream=stream)
    before = len(_lines(stream))
    log(get_logger("unit"), logging.INFO, "once")
    assert len(_lines(stream)) == before + 1


def test_log_level_is_respected():
    stream = io.StringIO()
    configure_logging("test", level="WARNING", fmt="json", stream=stream)
    get_logger("unit").info("quiet")
    get_logger("unit").warning("loud")
    messages = [r["message"] for r in _lines(stream)]
    assert "loud" in messages and "quiet" not in messages


def test_redact_is_pure_for_plain_values():
    assert redact(5) == 5 and redact(None) is None
    assert redact({"a": [1, {"token": "z"}]}) == {"a": [1, {"token": REDACTED}]}
