"""Regression tests: one failing/hanging backend must not block the hub.

Covers the OAuth-backed-backend scenario: a backend that hangs during
startup (e.g. waiting for interactive authorization) must be abandoned
after BACKEND_START_TIMEOUT while remaining backends start normally.
"""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from gearcore_hub.main import BACKEND_START_TIMEOUT, GearCoreHub
from gearcore_hub.process_manager import ProcessManager


def _make_hub(server_ids):
    """Build a GearCoreHub without running __init__ (no real backends)."""
    hub = GearCoreHub.__new__(GearCoreHub)
    cfgs = []
    for sid in server_ids:
        cfg = MagicMock()
        cfg.id = sid
        cfg.model_dump.return_value = {"id": sid}
        cfgs.append(cfg)
    hub.config = MagicMock()
    hub.config.mcp_servers = cfgs
    hub.process_manager = ProcessManager()
    return hub


@pytest.fixture(autouse=True)
def _short_timeout(monkeypatch):
    monkeypatch.setattr("gearcore_hub.main.BACKEND_START_TIMEOUT", 0.2)


def test_hanging_backend_does_not_block_others(caplog):
    async def scenario():
        hub = _make_hub(["slow-oauth", "healthy"])

        async def fake_register(cfg):
            if cfg["id"] == "slow-oauth":
                await asyncio.sleep(5.0)  # hangs far beyond the timeout
            hub.process_manager.servers[cfg["id"]] = MagicMock(session=MagicMock())

        hub.process_manager.register_and_start = fake_register

        # Must not raise despite the hanging backend.
        with caplog.at_level(logging.ERROR, logger="gearcore"):
            await hub._start_backends()

        return hub

    hub = asyncio.run(scenario())

    assert "slow-oauth" not in hub.process_manager.servers
    assert "healthy" in hub.process_manager.servers
    assert hub.process_manager.servers["healthy"].session is not None
    assert "slow-oauth" in hub.failed_backends
    assert "healthy" not in hub.failed_backends


def test_failing_backend_does_not_block_others():
    async def scenario():
        hub = _make_hub(["broken", "healthy"])

        async def fake_register(cfg):
            if cfg["id"] == "broken":
                raise RuntimeError("OAuth required")
            hub.process_manager.servers[cfg["id"]] = MagicMock(session=MagicMock())

        hub.process_manager.register_and_start = fake_register
        await hub._start_backends()
        return hub

    hub = asyncio.run(scenario())
    assert "broken" not in hub.process_manager.servers
    assert "healthy" in hub.process_manager.servers
    assert "broken" in hub.failed_backends


def test_hanging_backends_time_out_in_parallel():
    async def scenario():
        import time

        hub = _make_hub(["a", "b", "c"])

        async def fake_register(cfg):
            await asyncio.sleep(5.0)  # every backend hangs

        hub.process_manager.register_and_start = fake_register
        start = time.monotonic()
        await hub._start_backends()
        return time.monotonic() - start, hub

    elapsed, hub = asyncio.run(scenario())
    # Sequential would cost 3 x BACKEND_START_TIMEOUT (0.6s); parallel ~0.2s.
    assert elapsed < 0.45
    assert set(hub.failed_backends) == {"a", "b", "c"}


def test_shutdown_with_no_servers_is_clean():
    async def scenario():
        hub = _make_hub([])
        await hub.process_manager.shutdown_all()

    asyncio.run(scenario())


def test_default_timeout_is_fifteen_seconds():
    assert BACKEND_START_TIMEOUT == 15.0
