"""Credential-safe logs and quiet routine health probes."""
from __future__ import annotations

import logging
import re

_CREDS = re.compile(r"//([^/@:]+):([^/@]+)@")
_PARAM = re.compile(r"([?&](?:token|key|secret|auth|password|api_key|apikey|signature|sig)=)[^&#\s]*", re.I)


def redact(value: str) -> str:
    return _PARAM.sub(r"\1***", _CREDS.sub(r"//\1:***@", value))


class SafeLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if ('"GET /health ' in message or '"GET /healthz ' in message
                or "GET http://searxng:8080/healthz" in message):
            return False
        # Uvicorn's access formatter unpacks the original argument tuple.
        # Preserve its shape while redacting individual string fields.
        record.msg = redact(str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(redact(value) if isinstance(value, str) else value for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(value) if isinstance(value, str) else value for key, value in record.args.items()}
        return True


def configure_logging() -> None:
    for name in ("", "uvicorn.access", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            handler.addFilter(SafeLogFilter())
