"""Tests for SharedMCPServer lifecycle edge cases."""

import asyncio

import pytest

from gearcore_hub import process_manager as pm


class HangingClientCM:
    """Stand-in for stdio_client whose __aenter__ hangs until cancelled."""

    def __init__(self):
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        await asyncio.sleep(3600)

    async def __aexit__(self, *exc_info):
        self.exited = True
        return False


async def test_cancel_during_start_cleans_up_partial_init(monkeypatch):
    cm = HangingClientCM()
    monkeypatch.setattr(pm, "stdio_client", lambda params: cm)
    server = pm.SharedMCPServer("slow", transport="stdio", command="noop")

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(server.start(), timeout=0.1)

    assert cm.entered is True
    assert cm.exited is True, "partial init was not cleaned up after cancellation"
    assert server.session is None
    assert server._client_ctx is None
    assert server._streams is None
