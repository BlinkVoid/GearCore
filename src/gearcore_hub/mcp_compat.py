"""Compatibility shims for MCP client transport API drift."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


def _streamable_http_module() -> Any:
    return importlib.import_module("mcp.client.streamable_http")


def _http_client_module() -> Any:
    try:
        return importlib.import_module("httpx2")
    except ModuleNotFoundError:
        return importlib.import_module("httpx")


@asynccontextmanager
async def streamablehttp_client(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> AsyncIterator[Any]:
    """Open a streamable HTTP client across MCP 1.x and 2.x APIs.

    MCP 1.26 exposes the historical ``streamablehttp_client`` helper accepting
    ``headers=`` directly. MCP 2.x removes that symbol and exposes
    ``streamable_http_client`` instead; headers must be carried by an explicit
    HTTP client. This adapter keeps GearCore's transport boundary stable and
    fail-closed: bearer headers are still passed to the underlying HTTP layer.
    """

    module = _streamable_http_module()
    legacy_client = getattr(module, "streamablehttp_client", None)
    if legacy_client is not None:
        if headers is None:
            async with legacy_client(url, **kwargs) as streams:
                yield streams
        else:
            async with legacy_client(url, headers=headers, **kwargs) as streams:
                yield streams
        return

    modern_client = getattr(module, "streamable_http_client", None)
    if modern_client is None:
        raise RuntimeError("MCP streamable HTTP client transport is unavailable")

    if headers is None:
        async with modern_client(url, **kwargs) as streams:
            yield streams
        return

    http_client_module = _http_client_module()
    async with (
        http_client_module.AsyncClient(headers=dict(headers)) as http_client,
        modern_client(url, http_client=http_client, **kwargs) as streams,
    ):
        yield streams
