import json
import logging

import pytest
import structlog
import structlog.testing

from logs.logger_config import (
    DEFAULT_LOG_FILE,
    LOG_DIR,
    _JSONLineFormatter,
    log_file_for,
    logged_job,
    setup_production_logging,
)


def reset():
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


def test_formatter_returns_message_only():
    record = logging.LogRecord("n", logging.INFO, "f", 1, '{"a": 1}', None, None)
    assert _JSONLineFormatter().format(record) == '{"a": 1}'


def test_setup_writes_json_lines_and_creates_folder(tmp_path):
    log_file = tmp_path / "sub" / "app.log"
    try:
        setup_production_logging(log_file, cache_logger=False)
        structlog.get_logger("t").info("hello", n=1)
        logging.getLogger().handlers[0].flush()
        entry = json.loads(log_file.read_text().splitlines()[0])
        assert entry["event"] == "hello"
        assert entry["n"] == 1
        assert entry["level"] == "info"
    finally:
        reset()


def test_exception_traceback_stays_inside_json(tmp_path):
    log_file = tmp_path / "app.log"
    try:
        setup_production_logging(log_file, cache_logger=False)
        try:
            raise ValueError("boom")
        except ValueError:
            structlog.get_logger("t").exception("failed")
        logging.getLogger().handlers[0].flush()
        lines = log_file.read_text().splitlines()
        assert len(lines) == 1
        assert "exception" in json.loads(lines[0])
    finally:
        reset()


def test_logged_job_binds_context_and_logs_lifecycle():
    with structlog.testing.capture_logs() as logs:
        with structlog.contextvars.bound_contextvars(x=1):
            with logged_job("demo") as run_id:
                assert len(run_id) == 8
                structlog.get_logger("t").info("inside")
    assert [e["event"] for e in logs] == ["job_started", "inside", "job_finished"]
    assert "elapsed_seconds" in logs[-1]


def test_logged_job_context_is_removed_afterwards(tmp_path):
    try:
        setup_production_logging(tmp_path / "app.log", cache_logger=False)
        with logged_job("demo") as run_id:
            structlog.get_logger("t").info("inside")
        structlog.get_logger("t").info("outside")
        logging.getLogger().handlers[0].flush()
        entries = [json.loads(line) for line in (tmp_path / "app.log").read_text().splitlines()]
        by_event = {e["event"]: e for e in entries}
        assert by_event["inside"]["job"] == "demo"
        assert by_event["inside"]["run_id"] == run_id
        assert "job" not in by_event["outside"]
    finally:
        reset()


def test_logged_job_logs_failure_and_reraises():
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(RuntimeError):
            with logged_job("demo"):
                raise RuntimeError("boom")
    assert [e["event"] for e in logs] == ["job_started", "job_failed"]
    assert logs[-1]["log_level"] == "error"


def test_default_log_file_is_in_project_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    try:
        setup_production_logging(cache_logger=False)
        handler_path = logging.getLogger().handlers[0].baseFilename
        assert handler_path == str(DEFAULT_LOG_FILE)
        assert not (tmp_path / "logs").exists()
    finally:
        reset()


def test_log_file_for_is_one_file_per_job_in_common_folder():
    assert log_file_for("a") == LOG_DIR / "a.log"
    assert log_file_for("a") != log_file_for("b")
    assert log_file_for("a").parent == DEFAULT_LOG_FILE.parent
