"""
The one way to configure logging: `logging.basicConfig` with every handler that ends
up writing a line given `RedactingFormatter` (app/redact.py, GitHub issue #14).

A secret reached the container log three ways that a call-site `redact_secrets` never
covers: an f-string interpolating a transport error (the Flex `t=` URL), the traceback
`exc_info` appends (a sanitised `RuntimeError(...) from e` still prints the chained
`e`), and uvicorn's own `uvicorn.error` logger, which has its own handler and
`propagate=False` and logs the same traceback again after Starlette re-raises the 500.
Redacting the formatted text covers all three at once.

`app/main.py` and every CLI under `app/cli/` call this rather than `basicConfig`;
`tests/test_log_redaction.py` walks `app/` for a bare `basicConfig` call.
"""
import logging
from typing import IO, Optional

from app.redact import RedactingFormatter

# Uvicorn's `LOGGING_CONFIG` puts its own handlers on these (propagate=False), and it
# configures them before it imports the app, so they already exist when main runs this.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_logging(level: int, format: str, stream: Optional[IO[str]] = None) -> None:
    """
    `basicConfig(level, format, stream, force=True)`, then a redacting formatter on the
    root handler and on whatever handlers uvicorn installed.

    `force=True` because uvicorn installs handlers before the app is imported and
    basicConfig is a no-op whenever the root logger already has one. `stream=None` is
    basicConfig's own default (stderr), which is what the CLIs always had.
    """
    logging.basicConfig(level=level, format=format, stream=stream, force=True)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(format))
    for name in _UVICORN_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            if not isinstance(handler.formatter, RedactingFormatter):
                handler.setFormatter(
                    RedactingFormatter(inner=handler.formatter or logging.Formatter())
                )
