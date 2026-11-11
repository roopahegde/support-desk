"""Backend logging: the one place that decides where log lines go and what they look like.

Settings come from environment variables, normally set in backend/.env:
  LOG_TARGET  console (default) or file
  LOG_FILE    the log file when LOG_TARGET=file. Default: backend/logs/backend.log.
              A relative path is relative to the backend folder.
  LOG_LEVEL   DEBUG, INFO (default), WARNING or ERROR

Lines are plain text with key=value fields: readable by people, and easy for tools or an
AI model to parse. Every line carries the ID of the request it belongs to, for example:
  2026-11-11 14:30:02 INFO    [req=3f9a1c2b7d10] backend.app.main: refund_created order_id=1042
"""

import contextvars
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOG_FILE = BACKEND_DIR / "logs" / "backend.log"
LOG_FORMAT = "%(asctime)s %(levelname)-7s [req=%(request_id)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
HANDLER_NAME = "backend-log-handler"  # lets a reconfigure find and replace our own handler

# The current request's ID. A context variable gives each request its own value, even
# when many requests are handled at the same time.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Stamp each log record with the current request's ID ("-" outside a request)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True  # never drop a record; this filter only adds a field


def resolve_log_file(value: str | Path | None) -> Path:
    """Turn the LOG_FILE setting into a path. Relative paths are relative to backend/."""
    path = Path(value) if value else DEFAULT_LOG_FILE
    return path if path.is_absolute() else BACKEND_DIR / path


def configure_logging(
    target: str | None = None, log_file: str | Path | None = None, level: str | None = None
) -> logging.Handler:
    """Send all backend.* log lines to the console or a file. Arguments override the env.

    Safe to call again: it replaces the handler it added last time instead of adding a
    second one, which would print every line twice.
    """
    target = (target or os.environ.get("LOG_TARGET") or "console").lower()
    level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()

    if target == "console":
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
    elif target == "file":
        path = resolve_log_file(log_file or os.environ.get("LOG_FILE"))
        path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate at 5 MB and keep 5 old files, so the log can never fill the disk.
        handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    else:
        raise ValueError(f"LOG_TARGET must be 'console' or 'file', not {target!r}")

    handler.set_name(HANDLER_NAME)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    handler.addFilter(RequestIdFilter())

    logger = logging.getLogger("backend")  # the parent of every backend.app.* module logger
    for old in [h for h in logger.handlers if h.get_name() == HANDLER_NAME]:
        logger.removeHandler(old)
        old.close()  # releases the file, which Windows needs before it can be moved or deleted
    logger.addHandler(handler)
    logger.setLevel(level)
    return handler
