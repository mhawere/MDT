"""Smoke tests for MDT config, API routes, and test framework."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import config
from app.main import app, _adapt_startup_resources, _mem_available_mb
from app import apk_tests


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_config_defaults():
    assert config.MAX_DEVICES == 2
    assert config.EMULATOR_MEMORY_MB == 1536
    assert config.SCREENRECORD_BITRATE == 2_000_000
    assert config.SCREENRECORD_SIZE == "540x1170"


def test_emulator_serial_ports():
    assert config.emulator_serial(0) == "emulator-5554"
    assert config.emulator_serial(1) == "emulator-5556"
    assert config.emulator_ports(0) == (5554, 5555)


def test_mem_available_mb_positive():
    assert _mem_available_mb() > 0


def test_adapt_startup_resources_no_crash():
    original = config.EMULATOR_MEMORY_MB
    try:
        count = _adapt_startup_resources(2)
        assert count == 2
        assert config.EMULATOR_MEMORY_MB >= 1024
    finally:
        config.EMULATOR_MEMORY_MB = original


def test_list_available_tests():
    tests = apk_tests.list_available_tests()
    assert "launch" in tests
    assert "crash_detection" in tests
    assert len(tests) == 8


@pytest.mark.anyio
async def test_api_config():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/config")
    assert res.status_code == 200
    data = res.json()
    assert data["max_devices"] == 2
    assert "launch" in data["tests"]


@pytest.mark.anyio
async def test_api_devices_empty():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/devices")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


@pytest.mark.anyio
async def test_invalid_device_returns_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/device/99/restart_app")
    assert res.status_code == 404


@pytest.mark.anyio
async def test_invalid_apk_dir_returns_400():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/apk_dir", json={"path": "/nonexistent/path/xyz"})
    assert res.status_code == 400


@pytest.mark.anyio
async def test_unknown_test_returns_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/device/0/tests/not_a_real_test")
    assert res.status_code == 404
