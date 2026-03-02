# log_config.py

import logging
import os
import sys
from uvicorn.logging import ColourizedFormatter
import time

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
    # (clients, security_config, packs_router, etc.) gets timestamps.
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