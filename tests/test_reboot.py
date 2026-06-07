"""Tests for reboot flow, boot-wait logic, and loop prevention."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import config
from app.emulator import wait_for_boot, wait_for_disconnect
from app.main import _wait_and_relaunch, app
from app.state import DeviceState, app_state


@pytest.fixture
def device_state(tmp_path: Path) -> DeviceState:
    apk = tmp_path / "demo.apk"
    apk.write_bytes(b"PK fake apk")
    ds = DeviceState(
        index=7,
        serial="emulator-5554",
        avd_name="mdt_7",
        apk_path=apk,
        package="com.example.demo",
        state="running",
    )
    app_state.register_device(ds)
    yield ds
    app_state.devices = [d for d in app_state.devices if d.index != 7]


@pytest.mark.asyncio
async def test_wait_for_boot_after_reboot_waits_for_disconnect(device_state: DeviceState):
    disconnect = AsyncMock(return_value=True)
    boot_reads = iter(["0", "running", "1", "stopped"])

    async def fake_shell(*_args, **_kwargs):
        return next(boot_reads)

    with patch("app.emulator.wait_for_disconnect", disconnect), \
         patch("app.emulator._adb_shell_output", fake_shell), \
         patch("app.emulator.config.REBOOT_MIN_UPTIME_SEC", 0), \
         patch("app.emulator.asyncio.sleep", new=AsyncMock()), \
         patch("app.emulator.asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
        proc = AsyncMock()
        proc.wait = AsyncMock()
        spawn.return_value = proc

        ok = await wait_for_boot(device_state, after_reboot=True)

    assert ok is True
    disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_disconnect_detects_offline(device_state: DeviceState):
    states = ["device", "device", None]
    with patch("app.emulator._adb_get_state", AsyncMock(side_effect=states)), \
         patch("app.emulator.asyncio.sleep", new=AsyncMock()):
        ok = await wait_for_disconnect(device_state, timeout=2.0)
    assert ok is True


@pytest.mark.asyncio
async def test_wait_and_relaunch_skips_reinstall(device_state: DeviceState):
    device_state.reboot_task = asyncio.current_task()
    with patch("app.main.emulator.wait_for_boot", new=AsyncMock(return_value=True)), \
         patch("app.main.device.get_screen_size", new=AsyncMock(return_value=(1080, 2340))), \
         patch("app.main.device.install_apk", new=AsyncMock()) as install, \
         patch("app.main.device.launch_app", new=AsyncMock(return_value=True)) as launch, \
         patch("app.main._restart_logcat", new=AsyncMock()), \
         patch("app.main._restart_streamer", new=AsyncMock()), \
         patch("app.main._emit_state", new=AsyncMock()):
        await _wait_and_relaunch(device_state)

    install.assert_not_awaited()
    launch.assert_awaited_once_with(device_state.serial, "com.example.demo")
    assert device_state.reboot_failures == 0
    assert device_state.reboot_task is None


@pytest.mark.asyncio
async def test_wait_and_relaunch_stops_after_max_failures(device_state: DeviceState):
    device_state.reboot_failures = config.REBOOT_MAX_ATTEMPTS - 1
    device_state.reboot_task = asyncio.current_task()
    with patch("app.main.emulator.wait_for_boot", new=AsyncMock(return_value=False)), \
         patch("app.main._emit_state", new=AsyncMock()) as emit, \
         patch("app.main.activity_log_mod.activity_log.emit", new=AsyncMock()) as log_emit:
        await _wait_and_relaunch(device_state)

    assert device_state.reboot_failures == config.REBOOT_MAX_ATTEMPTS
    emit.assert_awaited()
    assert emit.await_args[0][1] == "error"
    log_emit.assert_awaited()
    assert device_state.reboot_task is None


@pytest.mark.asyncio
async def test_reboot_endpoint_rejects_concurrent_reboot(device_state: DeviceState):
    device_state.reboot_task = asyncio.create_task(asyncio.sleep(60))
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(f"/api/device/{device_state.index}/reboot")
        assert res.status_code == 409
    finally:
        device_state.reboot_task.cancel()
        try:
            await device_state.reboot_task
        except asyncio.CancelledError:
            pass
        device_state.reboot_task = None


@pytest.mark.asyncio
async def test_live_reload_skips_sync_while_booting(device_state: DeviceState, tmp_path: Path):
    from app.live_reload import LiveReloadState, sync_now

    apk = tmp_path / "app-debug.apk"
    apk.write_bytes(b"PK")
    device_state.state = "booting"
    lr = LiveReloadState(enabled=True, watch_path=apk, mode="apk", status="watching")

    with pytest.raises(RuntimeError, match="not ready"):
        await sync_now(device_state, lr)
