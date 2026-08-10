from __future__ import annotations

from contextlib import asynccontextmanager
from types import ModuleType
from typing import Any

import pytest

from gearcore_hub import mcp_compat


@pytest.mark.asyncio
async def test_streamablehttp_client_uses_legacy_headers_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None]] = []

    @asynccontextmanager
    async def legacy_client(url: str, *, headers: dict[str, str] | None = None):
        calls.append((url, headers))
        yield ("read", "write", lambda: "session")

    @asynccontextmanager
    async def modern_client(*_args: Any, **_kwargs: Any):
        raise AssertionError("legacy streamablehttp_client should be preferred")
        yield

    module = ModuleType("mcp.client.streamable_http")
    module.streamablehttp_client = legacy_client
    module.streamable_http_client = modern_client
    monkeypatch.setattr(mcp_compat, "_streamable_http_module", lambda: module)

    async with mcp_compat.streamablehttp_client(
        "https://dispatcher.invalid/mcp",
        headers={"Authorization": "Bearer sentinel"},
    ) as streams:
        assert streams[:2] == ("read", "write")
        assert streams[2]() == "session"

    assert calls == [
        (
            "https://dispatcher.invalid/mcp",
            {"Authorization": "Bearer sentinel"},
        )
    ]


@pytest.mark.asyncio
async def test_streamablehttp_client_preserves_headers_with_modern_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    @asynccontextmanager
    async def modern_client(url: str, *, http_client: Any):
        calls["url"] = url
        calls["headers"] = http_client.headers
        calls["entered"] = http_client.entered
        yield ("read", "write")

    class AsyncClient:
        def __init__(self, *, headers: dict[str, str]) -> None:
            self.headers = dict(headers)
            self.entered = False
            self.exited = False

        async def __aenter__(self) -> AsyncClient:
            self.entered = True
            calls["client"] = self
            return self

        async def __aexit__(self, *_exc: object) -> None:
            self.exited = True

    module = ModuleType("mcp.client.streamable_http")
    module.streamable_http_client = modern_client
    httpx2 = ModuleType("httpx2")
    httpx2.AsyncClient = AsyncClient
    monkeypatch.setattr(mcp_compat, "_streamable_http_module", lambda: module)
    monkeypatch.setitem(__import__("sys").modules, "httpx2", httpx2)

    async with mcp_compat.streamablehttp_client(
        "https://dispatcher.invalid/mcp",
        headers={"Authorization": "Bearer sentinel"},
    ) as streams:
        assert streams == ("read", "write")

    assert calls["url"] == "https://dispatcher.invalid/mcp"
    assert calls["headers"] == {"Authorization": "Bearer sentinel"}
    assert calls["entered"] is True
    assert calls["client"].exited is True
