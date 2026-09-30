"""Network-free coverage of SearXNG query semantics."""
from __future__ import annotations

import asyncio

import bridge.searxng_client as client
import httpx


def test_empty_dated_search_does_not_drop_filter(monkeypatch):
    calls = []

    class FakeClient:
        async def get(self, url, *, params):
            calls.append(params)
            return httpx.Response(200, json={"results": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(client, "_get_client", lambda: FakeClient())
    assert asyncio.run(client.search("q", time_range="week"))["results"] == []
    assert len(calls) == 1
    assert calls[0]["time_range"] == "week"
