"""Credential-bearing inputs cannot leak through Bridge log handlers."""
from __future__ import annotations

import logging

from bridge.logging_utils import SafeLogFilter


def test_log_filter_redacts_urls_and_query_arguments():
    record = logging.LogRecord("bridge", logging.INFO, "", 0, "Scrape failed %s: %s",
                               ("https://user:pass@example.com?token=secret", "https://proxy:x@proxy.test?key=value"), None)
    assert SafeLogFilter().filter(record)
    message = record.getMessage()
    assert "pass" not in message and "secret" not in message and "value" not in message
    assert "***" in message


def test_log_filter_suppresses_routine_health_requests():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '127.0.0.1 "GET /health HTTP/1.1" 200', (), None)
    assert not SafeLogFilter().filter(record)


def test_access_formatter_keeps_required_argument_tuple():
    from uvicorn.logging import AccessFormatter

    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1", "GET", "/scrape?token=secret", "1.1", 200), None)
    assert SafeLogFilter().filter(record)
    output = AccessFormatter('%(client_addr)s "%(request_line)s" %(status_code)s').format(record)
    assert "secret" not in output
    assert "200" in output


def test_health_failures_remain_logged():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1", "GET", "/health", "1.1", 503), None)
    assert SafeLogFilter().filter(record)
