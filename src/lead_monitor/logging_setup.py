"""Structured logging with secret redaction.

Logs go to stdout as one JSON object per line, which GitHub Actions renders
readably and which stays greppable if the output is ever shipped elsewhere. A
``LOG_PLAIN=true`` escape hatch gives human-friendly lines for local work.

The redaction filter matters more than it looks. Portal and SMTP passwords have
a habit of surfacing in exception messages, request URLs and library-level debug
output, and CI logs for a public repository are world-readable. Rather than trust
every dependency to be careful, every record is scrubbed on the way out.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_REDACTED = "***REDACTED***"

# Records carry these attributes as standard; anything else was supplied by the
# caller via `extra=` and is worth emitting alongside the message.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class SecretRedactingFilter(logging.Filter):
    """Replace known secret values anywhere in a record before it is emitted."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        # Longest first, so a secret that contains another is masked whole.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True

        record.msg = self._scrub(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._scrub(str(v)) for k, v in record.args.items()}
            else:
                record.args = tuple(self._scrub(str(a)) for a in record.args)

        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD_ATTRS and isinstance(value, str):
                record.__dict__[key] = self._scrub(value)

        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including any ``extra=`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO",
    *,
    plain: bool = False,
    secrets: list[str] | None = None,
) -> None:
    """Install the root handler. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s %(name)s  %(message)s")
        if plain
        else JsonFormatter()
    )
    handler.addFilter(SecretRedactingFilter(secrets or []))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # These are chatty at DEBUG and their output can include credentials.
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
