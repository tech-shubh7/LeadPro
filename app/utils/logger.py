"""Logging for the whole application: one configuration, a module-named logger everywhere.

Every module does `logger = get_logger(__name__)`; entry points (the server, the CLI) call
`configure_logging()` once. Libraries' own loggers are left to their defaults.
"""

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_ROOT = "app"


def configure_logging(level: str = "INFO") -> None:
    """Idempotent: safe to call from more than one entry point."""
    root = logging.getLogger(_ROOT)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))
        root.addHandler(handler)
        root.propagate = False
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith(_ROOT) else f"{_ROOT}.{name}")
