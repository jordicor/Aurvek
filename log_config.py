# log_config.py

import logging
import os
import sys
from uvicorn.logging import ColourizedFormatter
import time
from contextlib import contextmanager, redirect_stdout
from urllib.parse import unquote, urlsplit


def sanitize_auth_request_target(target: str) -> str:
    """Keep access diagnostics without recording browser auth credentials."""
    try:
        parsed = urlsplit(target)
    except ValueError:
        return target
    path = unquote(parsed.path)
    if path == "/verify-email" or path.startswith("/verify-email/"):
        return "/verify-email/[redacted]"
    sensitive = (
        path.rstrip("/") in {"/login", "/register", "/magic-link-recovery"}
        or path.startswith("/auth/google")
        or path == "/embed" or path.startswith("/embed/")
        or path == "/api/embed/v1" or path.startswith("/api/embed/v1/")
    )
    if sensitive:
        return parsed.path
    return target


class AuthAccessLogFilter(logging.Filter):
    """Sanitize Uvicorn's structured request-target argument before formatting."""

    def filter(self, record):
        if (record.name == "uvicorn.access" and isinstance(record.args, tuple)
                and len(record.args) == 5 and isinstance(record.args[2], str)):
            args = list(record.args)
            args[2] = sanitize_auth_request_target(args[2])
            record.args = tuple(args)
        return True

class CustomColourizedFormatter(ColourizedFormatter):
    def format(self, record):
        record.asctime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        if record.levelno == logging.ERROR:
            record.msg = f"!!! {record.msg}"
        return super().format(record)

def setup_logging():
    # Main application logger configuration
    logger = logging.getLogger("app")
    _app_debug = os.getenv("APP_DEBUG", "false").lower() == "true"
    logger.setLevel(logging.DEBUG if _app_debug else logging.INFO)
    logger.propagate = False  # Prevent log propagation to root logger

    # Uvicorn loggers configuration
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_asgi = logging.getLogger("uvicorn.asgi")
    # Keep this on the logger so every handler receives the sanitized record.
    if not any(isinstance(item, AuthAccessLogFilter) for item in uvicorn_access.filters):
        uvicorn_access.addFilter(AuthAccessLogFilter())

    # Clear existing handlers
    for log in [logger, uvicorn_error, uvicorn_access, uvicorn_asgi]:
        log.handlers = []

    # Configure the log format
    log_format = "%(asctime)s - %(levelprefix)s %(message)s"
    formatter = CustomColourizedFormatter(log_format, use_colors=True)

    # Configure the handler for the console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # Add handler to all loggers
    for log in [logger, uvicorn_error, uvicorn_access, uvicorn_asgi]:
        log.addHandler(console_handler)

    # Apply the same formatter to the root logger so every module
    # (clients, security_config, marketplace routes, etc.) gets timestamps.
    root = logging.getLogger()
    root.handlers = []
    root.addHandler(console_handler)
    root.setLevel(logging.INFO)

    # Configure the logging level for Uvicorn loggers
    for log in [uvicorn_error, uvicorn_access, uvicorn_asgi]:
        log.setLevel(logging.INFO)

    # Disable propagation for Uvicorn loggers
    logging.getLogger("uvicorn").propagate = False

    return logger

# Create and configure the logger
logger = setup_logging()


@contextmanager
def cli_diagnostics_to_stderr():
    """Keep JSON CLI stdout clean without changing normal application logging."""
    stdout = sys.stdout
    loggers = [logging.getLogger(), *(item for item in logging.Logger.manager.loggerDict.values()
                                     if isinstance(item, logging.Logger))]
    handlers = {handler for log in loggers for handler in log.handlers
                if getattr(handler, "stream", None) is stdout}
    for handler in handlers:
        handler.setStream(sys.stderr)
    try:
        with redirect_stdout(sys.stderr):
            yield
    finally:
        for handler in handlers:
            handler.setStream(stdout)
