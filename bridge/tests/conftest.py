"""Keep operational caches and breakers independent of test order."""
from __future__ import annotations

import pytest

from bridge import main, searxng_client


@pytest.fixture(autouse=True)
def isolate_search_state(monkeypatch):
    monkeypatch.setattr(main, "_search_cache", {})
    monkeypatch.setattr(searxng_client, "_failures", 0)
    monkeypatch.setattr(searxng_client, "_skip_until", 0.0)
    # The deployed image's .env may enable the optional Ceramic fallback —
    # tests that exercise it pin it explicitly (deployment-config rule, AGENTS.md).
    monkeypatch.setattr(main, "CERAMIC_SEARCH_FALLBACK", False)
