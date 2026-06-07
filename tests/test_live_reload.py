"""Tests for live reload path validation, debouncing, and enable/disable state."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import config
from app.live_reload import (
    LiveReloadState,
    detect_changes,
    disable,
    enable,
    get_state,
    resolve_apk_target,
    sync_now,
    validate_watch_path,
    _debounced_sync,
    _file_snapshot,
)
from app.state import DeviceState, app_state


@pytest.fixture
def tmp_apk(tmp_path: Path) -> Path:
    apk = tmp_path / "app-debug.apk"
    apk.write_bytes(b"PK fake apk")
    return apk


@pytest.fixture
def tmp_assets(tmp_path: Path) -> Path:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "data.json").write_text('{"v":1}')
    return assets


@pytest.fixture
def device_state(tmp_apk: Path) -> DeviceState:
    ds = DeviceState(
        index=99,
        serial="emulator-5554",
        avd_name="mdt_99",
        apk_path=tmp_apk,
        package="com.example.app",
        state="running",
    )
    app_state.register_device(ds)
    yield ds
    app_state.devices = [d for d in app_state.devices if d.index != 99]


def test_validate_watch_path_apk_file(tmp_apk: Path):
    p = validate_watch_path(tmp_apk, "apk")
    assert p == tmp_apk.resolve()


def test_validate_watch_path_apk_dir(tmp_path: Path, tmp_apk: Path):
    p = validate_watch_path(tmp_path, "apk")
    assert p == tmp_path.resolve()


def test_validate_watch_path_rejects_missing(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        validate_watch_path(tmp_path / "nope.apk", "apk")


def test_validate_watch_path_rejects_non_apk_file(tmp_path: Path):
    f = tmp_path / "readme.txt"
    f.write_text("hi")
    with pytest.raises(ValueError, match="requires a .apk"):
        validate_watch_path(f, "apk")


def test_validate_watch_path_assets_requires_dir(tmp_apk: Path):
    with pytest.raises(ValueError, match="requires a directory"):
        validate_watch_path(tmp_apk, "assets")


def test_validate_watch_path_assets_ok(tmp_assets: Path):
    p = validate_watch_path(tmp_assets, "assets")
    assert p == tmp_assets.resolve()


def test_resolve_apk_target_file(tmp_apk: Path):
    assert resolve_apk_target(tmp_apk, None) == tmp_apk


def test_resolve_apk_target_dir_by_name(tmp_path: Path, tmp_apk: Path):
    other = tmp_path / "other.apk"
    other.write_bytes(b"PK")
    assert resolve_apk_target(tmp_path, tmp_apk) == tmp_apk


def test_resolve_apk_target_dir_first_sorted(tmp_path: Path):
    b = tmp_path / "b.apk"
    a = tmp_path / "a.apk"
    b.write_bytes(b"B")
    a.write_bytes(b"A")
    assert resolve_apk_target(tmp_path, None) == a


def test_file_snapshot_and_detect_changes(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("a")
    snap1 = _file_snapshot(tmp_path)
    assert "x.txt" in snap1

    time.sleep(0.05)
    f.write_text("b")
    snap2 = _file_snapshot(tmp_path)
    changed = detect_changes(snap1, snap2)
    assert "x.txt" in changed


def test_detect_changes_ignores_unchanged(tmp_path: Path):
    f = tmp_path / "y.txt"
    f.write_text("same")
    snap = _file_snapshot(tmp_path)
    assert detect_changes(snap, snap) == []


@pytest.mark.asyncio
async def test_enable_disable_state(device_state: DeviceState, tmp_apk: Path):
    lr = await enable(device_state, tmp_apk, mode="apk")
    assert lr.enabled is True
    assert lr.status == "watching"
    assert lr.watch_path == tmp_apk.resolve()

    stopped = await disable(device_state.index)
    assert stopped.enabled is False
    assert stopped.status == "stopped"
    assert stopped.watcher_task is None


@pytest.mark.asyncio
async def test_enable_requires_assets_remote_path(device_state: DeviceState, tmp_assets: Path):
    with pytest.raises(ValueError, match="remote_path"):
        await enable(device_state, tmp_assets, mode="assets")


@pytest.mark.asyncio
async def test_debounced_sync_waits(monkeypatch, device_state: DeviceState, tmp_apk: Path):
    monkeypatch.setattr("app.live_reload.LIVE_RELOAD_DEBOUNCE_SEC", 0.2)
    lr = LiveReloadState(enabled=True, watch_path=tmp_apk, mode="apk", status="watching")

    sync_mock = AsyncMock()
    with patch("app.live_reload.sync_now", sync_mock):
        t0 = time.monotonic()
        await _debounced_sync(device_state, lr, "app-debug.apk")
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.15
        sync_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_apk_installs_and_launches(device_state: DeviceState, tmp_apk: Path):
    lr = LiveReloadState(enabled=True, watch_path=tmp_apk, mode="apk", status="watching")

    with patch("app.live_reload.device.install_apk", new=AsyncMock(return_value=(True, "Success"))) as inst, \
         patch("app.live_reload.device.adb_shell", new=AsyncMock(return_value="")) as shell, \
         patch("app.live_reload.device.launch_app", new=AsyncMock(return_value=True)) as launch:
        result = await sync_now(device_state, lr)

    assert result["ok"] is True
    assert result["mode"] == "apk"
    inst.assert_awaited_once()
    shell.assert_awaited_once()
    launch.assert_awaited_once()
    assert lr.sync_count == 1
    assert lr.status == "watching"


@pytest.mark.asyncio
async def test_sync_apk_failure_sets_error(device_state: DeviceState, tmp_apk: Path):
    lr = LiveReloadState(enabled=True, watch_path=tmp_apk, mode="apk", status="watching")

    with patch("app.live_reload.device.install_apk", new=AsyncMock(return_value=(False, "Failure"))):
        with pytest.raises(RuntimeError, match="adb install failed"):
            await sync_now(device_state, lr)

    assert lr.status == "error"
    assert "adb install failed" in lr.last_error


@pytest.mark.asyncio
async def test_sync_assets_pushes_files(device_state: DeviceState, tmp_assets: Path):
    lr = LiveReloadState(
        enabled=True,
        watch_path=tmp_assets,
        mode="assets",
        remote_path="/sdcard/myapp",
        status="watching",
    )

    with patch("app.live_reload.device.push_file", new=AsyncMock(return_value=(True, "pushed"))) as push:
        result = await sync_now(device_state, lr)

    assert result["ok"] is True
    assert result["mode"] == "assets"
    push.assert_awaited()


@pytest.mark.asyncio
async def test_get_state_isolated_per_index():
    s0 = get_state(0)
    s1 = get_state(1)
    s0.enabled = True
    assert s1.enabled is False


@pytest.mark.asyncio
async def test_api_live_reload_status_404():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/device/88/live-reload/status")
    assert res.status_code == 404
