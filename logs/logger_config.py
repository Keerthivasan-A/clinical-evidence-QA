import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Anchored to the project, not the working directory, so the log lands in the
# same file whether a script is run from the root, from etl/ or from an IDE.
LOG_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_FILE = LOG_DIR / "app.log"


def log_file_for(job: str) -> Path:
    """Return the log file of one job, inside the common log folder.

    Args:
        job: Stable job name, the same one passed to logged_job.

    Returns:
        logs/<job>.log in the project root.
    """
    return LOG_DIR / f"{job}.log"


class _JSONLineFormatter(logging.Formatter):
    """Write only the JSON string structlog produced, nothing else.

    The default formatter appends a plain-text traceback when a log call has
    exception info (e.g. logger.exception). structlog's dict_tracebacks has
    already put the traceback inside the JSON, so the text copy is dropped to
    keep the file as one valid JSON object per line.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Return the record's message (already JSON) with nothing appended.

        Args:
            record: The log record produced by structlog's stdlib bridge.

        Returns:
            The JSON line to write.
        """
        return record.getMessage()


def setup_production_logging(
        log_filename: str | Path = DEFAULT_LOG_FILE,
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
            Defaults to logs/app.log in the project root, whatever the
            current working directory is.
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


@contextmanager
def logged_job(job: str) -> Iterator[str]:
    """Wrap one run of a script so every log line carries the same job context.

    Binds `job` and a short random `run_id` to all log lines written inside the
    block (from any module), then logs `job_started`, and either `job_finished`
    or `job_failed` (with the traceback) together with `elapsed_seconds`. The
    exception is re-raised, so the script still exits with an error.

    Use it in a script's `main()`, after `setup_production_logging()` ran.

    Args:
        job: Stable name of the script, e.g. "download_pubmed_docs".

    Yields:
        The run id, useful for finding one run in a log file shared by many.
    """
    run_id = uuid.uuid4().hex[:8]
    start = time.perf_counter()
    with structlog.contextvars.bound_contextvars(job=job, run_id=run_id):
        logger.info("job_started")
        try:
            yield run_id
        except Exception:
            logger.exception("job_failed", elapsed_seconds=round(time.perf_counter() - start, 2))
            raise
        logger.info("job_finished", elapsed_seconds=round(time.perf_counter() - start, 2))
