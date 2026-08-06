"""Unit tests for the MCP server helpers (no network required).

Run inside the mcp container:  python -m pytest tests -q
or via CI / `make test-unit`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server as server_mod


def test_format_search_results():
    result = {
        "number_of_results": 2,
        "results": [
            {"title": "Foo", "url": "https://foo.com", "content": "snippet text"},
            {"title": "Bar", "url": "https://bar.com"},
        ],
    }
    out = server_mod._format_search_results(result)
    assert "Foo" in out and "https://foo.com" in out
    assert "snippet text" in out
    assert "Bar" in out


def test_format_scrape_result_markdown():
    result = {"url": "https://a.com", "title": "T", "markdown": "# hello", "tables": [{"rows": []}]}
    out = server_mod._format_scrape_result(result)
    assert "# hello" in out and "1 table(s)" in out


def test_format_combined_results_markdown_content():
    result = {
        "query": "q",
        "results": [
            {"title": "A", "url": "https://a.com", "content": {"url": "https://a.com", "markdown": "full content"}},
        ],
    }
    out = server_mod._format_combined_results(result)
    assert "full content" in out


def test_auth_no_key_allows_everything():
    old = server_mod.MCP_API_KEY
    server_mod.MCP_API_KEY = ""
    try:
        assert server_mod._auth_ok({"client": ("10.0.0.5", 123), "headers": []}) is True
    finally:
        server_mod.MCP_API_KEY = old


def test_auth_with_key():
    old_key, old_cidrs = server_mod.MCP_API_KEY, server_mod._TRUSTED_CIDRS
    server_mod.MCP_API_KEY = "sekrit"
    server_mod._TRUSTED_CIDRS = {"127.0.0.0/8", "::1/128"}
    try:
        assert server_mod._auth_ok({"client": ("127.0.0.1", 5), "headers": []}) is True
        untrusted = {"client": ("10.0.0.5", 5), "headers": [(b"authorization", b"Bearer wrong")]}
        assert server_mod._auth_ok(untrusted) is False
        good = {"client": ("10.0.0.5", 5), "headers": [(b"authorization", b"Bearer sekrit")]}
        assert server_mod._auth_ok(good) is True
        assert server_mod._auth_ok({"client": ("10.0.0.5", 5), "headers": []}) is False
    finally:
        server_mod.MCP_API_KEY = old_key
        server_mod._TRUSTED_CIDRS = old_cidrs


def test_trusted_cidrs():
    old = server_mod._TRUSTED_CIDRS
    server_mod._TRUSTED_CIDRS = {"127.0.0.0/8", "10.20.30.0/24"}
    try:
        assert server_mod._is_trusted({"client": ("10.20.30.55", 1)}) is True
        assert server_mod._is_trusted({"client": ("10.20.31.55", 1)}) is False
        assert server_mod._is_trusted({"client": ("127.0.0.1", 1)}) is True
    finally:
        server_mod._TRUSTED_CIDRS = old
