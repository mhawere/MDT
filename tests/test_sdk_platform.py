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


@pytest.mark.parametrize(
    ("platform_name", "sdk_tool", "bin_tool"),
    [
        ("win", "sdkmanager.bat", "adb.exe"),
        ("linux", "sdkmanager", "adb"),
        ("mac", "sdkmanager", "adb"),
    ],
)
def test_tool_names_per_platform(platform_name: str, sdk_tool: str, bin_tool: str):
    with patch.object(sdk, "PLATFORM", platform_name):
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
