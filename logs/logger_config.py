import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


class _JSONLineFormatter(logging.Formatter):
    """Write only the JSON string structlog produced, nothing else.

    The default formatter appends a plain-text traceback when a log call has
    exception info (e.g. logger.exception). structlog's dict_tracebacks has
    already put the traceback inside the JSON, so the text copy is dropped to
    keep the file as one valid JSON object per line.
    """

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def setup_production_logging(
        log_filename: str | Path = "./logs/app.log",
        *,
        level: int = logging.INFO,
        cache_logger: bool = True,
) -> None:
    """Configure structlog to write JSON lines to a rotating log file.

    Call this once, at the application entry point (a script's
    `if __name__ == "__main__":` block or app startup). Library modules
    should only call `structlog.get_logger(__name__)`.

    Args:
        log_filename: Path of the log file. Its folder is created if missing.
        level: Minimum level to write, e.g. logging.INFO or logging.DEBUG.
        cache_logger: Cache each logger after first use for speed. Keep True
            in production. Tests pass False so they can reconfigure logging
            between tests without loggers holding on to an old setup.
    """
    log_path = Path(log_filename)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Define the production processing chain
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,  # which module logged it, e.g. "etl.pubmed_parser"
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer(),
    ]

    # 2. Configure a standard Python RotatingFileHandler
    # This creates the file, limits its size, and manages historical backups
    file_handler = RotatingFileHandler(
        filename=log_path,
        maxBytes=50 * 1024 * 1024,  # 50 MB per file limit
        backupCount=7,  # Keeps exactly 7 historical log files
        encoding="utf-8",
    )
    file_handler.setFormatter(_JSONLineFormatter())

    # 3. Use standard logging to bridge structlog to the file handler.
    # force=True replaces any handlers already on the root logger. Without it,
    # basicConfig silently does nothing if something (pytest, a notebook, a
    # library) has already attached a handler, and no log file gets written.
    logging.basicConfig(
        handlers=[file_handler],
        level=level,
        force=True,
    )

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),  # Routes logs through the file handler
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=cache_logger,
    )
