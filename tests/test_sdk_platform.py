"""Tests for cross-platform Android SDK tool path resolution."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import config
from app import sdk
import app.main as main_mod
from app.main import app
from app.sdk_config import (
    detect_sdk_candidates,
    find_cmdline_bin,
    is_project_local_sdk,
    load_saved_sdk_path,
    os_default_sdk_locations,
    resolve_sdk_root,
    save_sdk_path,
    set_sdk_path,
    validate_sdk,
)


def _fake_sdk_root(tmp_path: Path) -> Path:
    root = tmp_path / "fake-sdk"
    (root / "platform-tools").mkdir(parents=True)
    (root / "emulator").mkdir(parents=True)
    (root / "cmdline-tools" / "latest" / "bin").mkdir(parents=True)
    (root / "platform-tools" / "adb").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "emulator" / "emulator").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "cmdline-tools" / "latest" / "bin" / "sdkmanager").write_text("#!/bin/sh\n")
    (root / "cmdline-tools" / "latest" / "bin" / "avdmanager").write_text("#!/bin/sh\n")
    return root


@pytest.mark.parametrize(
    ("platform_name", "sdk_tool", "bin_tool"),
    [
        ("win", "sdkmanager.bat", "adb.exe"),
        ("linux", "sdkmanager", "adb"),
        ("mac", "sdkmanager", "adb"),
    ],
)
def test_tool_names_per_platform(platform_name: str, sdk_tool: str, bin_tool: str):
    with patch.object(sdk, "PLATFORM", platform_name), \
         patch("app.sdk.find_cmdline_bin", return_value=None):
        assert sdk._sdk_script_name("sdkmanager") == sdk_tool
        assert sdk._bin_name("adb") == bin_tool
        assert sdk.sdkmanager_path().name == sdk_tool
        assert sdk.avdmanager_path().name == ("avdmanager.bat" if platform_name == "win" else "avdmanager")
        assert sdk.adb_path().name == bin_tool
        assert sdk.emulator_bin_path().name == ("emulator.exe" if platform_name == "win" else "emulator")


def test_cmdline_tools_archive_per_platform():
    with patch.object(sdk, "PLATFORM", "linux"):
        assert "commandlinetools-linux-" in sdk._cmdline_tools_archive()
    with patch.object(sdk, "PLATFORM", "win"):
        assert "commandlinetools-win-" in sdk._cmdline_tools_archive()
    with patch.object(sdk, "PLATFORM", "mac"):
        assert "commandlinetools-mac-" in sdk._cmdline_tools_archive()


def test_imported_paths_match_current_platform():
    if sys.platform == "win32":
        assert sdk.SDKMANAGER.name == "sdkmanager.bat"
        assert sdk.AVDMANAGER.name == "avdmanager.bat"
        assert sdk.adb_path().name == "adb.exe"
    elif sys.platform.startswith("linux") or sys.platform == "darwin":
        assert sdk.SDKMANAGER.name == "sdkmanager"
        assert sdk.AVDMANAGER.name == "avdmanager"
        assert not str(sdk.SDKMANAGER).endswith(".bat")
        assert sdk.adb_path().name == "adb"
        assert sdk.emulator_bin_path().name == "emulator"


@pytest.mark.asyncio
async def test_apk_dir_does_not_use_bat_paths_on_linux(tmp_path: Path):
    """Selecting an APK folder must not invoke Windows-only .bat SDK tools."""
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux-specific regression test")

    from app.state import app_state

    apk_dir = tmp_path / "apks"
    apk_dir.mkdir()
    (apk_dir / "demo.apk").write_bytes(b"PK fake apk")

    from app.state import DeviceState

    saved_devices = list(app_state.devices)
    saved_mem = config.EMULATOR_MEMORY_MB
    saved_adapted = main_mod._resources_adapted
    app_state.devices = []
    old_apk = tmp_path / "old.apk"
    old_apk.write_bytes(b"PK old")
    ds = DeviceState(
        index=0,
        serial=config.emulator_serial(0),
        avd_name="mdt_0",
        active=True,
        apk_path=old_apk,
        package="com.example.old",
        state="running",
    )
    app_state.register_device(ds)
    try:
        with patch("app.main.apk_mod.resolve_package", return_value="com.example.demo"), \
             patch("app.main.emulator.stop_emulator", new=AsyncMock()), \
             patch("app.main.ensure_sdk_ready"), \
             patch("app.main.avd_mod.ensure_avd", return_value="mdt_0") as ensure_avd, \
             patch("app.main.emulator.launch_emulator") as launch, \
             patch("app.main.emulator.wait_for_boot", new=AsyncMock(return_value=False)):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                res = await client.post("/api/apk_dir", json={"path": str(apk_dir)})
                await asyncio.sleep(0.3)

        assert res.status_code == 200
        ensure_avd.assert_called_once()
        launch.assert_called_once()
        avd_tool = str(sdk.AVDMANAGER)
        assert not avd_tool.endswith(".bat"), f"Linux must not use .bat paths: {avd_tool}"
    finally:
        app_state.devices = saved_devices
        config.EMULATOR_MEMORY_MB = saved_mem
        main_mod._resources_adapted = saved_adapted


def test_find_cmdline_bin_latest(tmp_path: Path):
    root = _fake_sdk_root(tmp_path)
    found = find_cmdline_bin(root, "sdkmanager")
    assert found is not None
    assert found.name == "sdkmanager"


def test_validate_sdk_core_tools(tmp_path: Path):
    root = _fake_sdk_root(tmp_path)
    v = validate_sdk(root)
    assert v["valid"] is True
    assert v["ready"] is False
    assert v["tools"]["adb"]["found"] is True


def test_validate_sdk_missing_dir(tmp_path: Path):
    v = validate_sdk(tmp_path / "nope")
    assert v["valid"] is False


def test_resolve_sdk_saved_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    saved = _fake_sdk_root(tmp_path)
    monkeypatch.setattr("app.sdk_config.SDK_PATH_FILE", tmp_path / ".sdk_path.txt")
    save_sdk_path(saved)
    assert resolve_sdk_root() == saved.resolve()


def test_resolve_sdk_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_sdk = _fake_sdk_root(tmp_path)
    monkeypatch.delenv("MDT_ANDROID_SDK", raising=False)
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(env_sdk))
    monkeypatch.setattr("app.sdk_config.SDK_PATH_FILE", tmp_path / "missing.txt")
    assert resolve_sdk_root() == env_sdk.resolve()


def test_os_default_sdk_locations_non_empty():
    assert len(os_default_sdk_locations()) >= 1


def test_is_project_local_sdk():
    local = config.PROJECT_ROOT / config.SDK_DIR
    assert is_project_local_sdk(local) is True


def test_detect_sdk_candidates_includes_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.sdk_config.SDK_PATH_FILE", tmp_path / ".sdk_path.txt")
    for var in ("MDT_ANDROID_SDK", "ANDROID_SDK_ROOT", "ANDROID_HOME"):
        monkeypatch.delenv(var, raising=False)
    local = str((config.PROJECT_ROOT / config.SDK_DIR).resolve())
    roots = {c["sdk_root"] for c in detect_sdk_candidates()}
    assert local in roots


def test_set_sdk_path_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.sdk_config.SDK_PATH_FILE", tmp_path / ".sdk_path.txt")
    root = _fake_sdk_root(tmp_path)
    status = set_sdk_path(str(root))
    assert status["sdk_root"] == str(root.resolve())
    assert load_saved_sdk_path() == root.resolve()


@pytest.mark.asyncio
async def test_api_sdk_get():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/sdk")
    assert res.status_code == 200
    data = res.json()
    assert "sdk_root" in data
    assert "valid" in data


@pytest.mark.asyncio
async def test_api_sdk_detect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.sdk_config.SDK_PATH_FILE", tmp_path / ".sdk_path.txt")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/sdk/detect")
    assert res.status_code == 200
    assert "candidates" in res.json()


@pytest.mark.asyncio
async def test_api_sdk_set_invalid_path():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/sdk", json={"path": "/nonexistent/sdk/path/xyz"})
    assert res.status_code == 400
