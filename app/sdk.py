"""
sdk.py — Bootstrap the project-local Android SDK for Windows.

Downloads cmdline-tools if missing, accepts licenses, installs required packages,
and checks hardware acceleration at startup.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import config

CMDLINE_TOOLS_VERSION = "11076708"
OS = "win"
ABI = "x86_64"

SDKMANAGER = config.ANDROID_SDK_ROOT / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
AVDMANAGER = config.ANDROID_SDK_ROOT / "cmdline-tools" / "latest" / "bin" / "avdmanager.bat"


def ensure_system_prerequisites() -> None:
    if not shutil.which("java"):
        print("\n[MDT] ⚠  Missing system prerequisite: java")
        print("[MDT]    Install a JDK (17+) and ensure java.exe is on PATH, then re-run start.bat.")
        print("[MDT]    Example (PowerShell): winget install EclipseAdoptium.Temurin.17.JDK")
        print()
        sys.exit(1)


def _download_cmdline_tools() -> None:
    url = (
        f"https://dl.google.com/android/repository/"
        f"commandlinetools-win-{CMDLINE_TOOLS_VERSION}_latest.zip"
    )
    zip_path = config.ANDROID_SDK_ROOT / "cmdline-tools.zip"
    config.ANDROID_SDK_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"[MDT] Downloading Android cmdline-tools from:\n      {url}")
    try:
        urllib.request.urlretrieve(url, zip_path, _dl_progress)
    except Exception as exc:
        print(f"\n[MDT] ✗  Download failed: {exc}")
        print(f"[MDT]    URL tried: {url}")
        print(f"[MDT]    If this 404s, bump CMDLINE_TOOLS_VERSION in sdk.py.")
        sys.exit(1)

    print("\n[MDT] Extracting cmdline-tools …")
    tmp_dir = config.ANDROID_SDK_ROOT / "_cmdtmp"
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tmp_dir)

    extracted = tmp_dir / "cmdline-tools"
    dest = config.ANDROID_SDK_ROOT / "cmdline-tools" / "latest"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(extracted), str(dest))
    shutil.rmtree(tmp_dir, ignore_errors=True)
    zip_path.unlink(missing_ok=True)

    print("[MDT] cmdline-tools installed ✓")


def _dl_progress(block_num: int, block_size: int, total_size: int) -> None:
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(downloaded * 100 // total_size, 100)
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"\r  [{bar}] {pct:3d}%", end="", flush=True)


def _sdk_env() -> dict:
    env = os.environ.copy()
    env["ANDROID_SDK_ROOT"] = str(config.ANDROID_SDK_ROOT)
    env["ANDROID_HOME"] = str(config.ANDROID_SDK_ROOT)
    env["ANDROID_AVD_HOME"] = str(config.ANDROID_AVD_HOME)
    return env


def _run_sdkmanager(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    cmd = [str(SDKMANAGER), f"--sdk_root={config.ANDROID_SDK_ROOT}"] + list(args)
    return subprocess.run(
        cmd,
        env=_sdk_env(),
        input=input_text,
        capture_output=True,
        text=True,
    )


def _accept_licenses() -> None:
    print("[MDT] Accepting Android SDK licenses …")
    result = _run_sdkmanager("--licenses", input_text="y\n" * 20)
    if result.returncode not in (0, 1):
        print(f"[MDT]   (licenses stdout): {result.stdout[-400:]}")


def _package_installed(pkg: str) -> bool:
    mapping = {
        "platform-tools": config.ANDROID_SDK_ROOT / "platform-tools" / "adb.exe",
        "emulator": config.ANDROID_SDK_ROOT / "emulator" / "emulator.exe",
        f"platforms;android-{config.API_LEVEL}":
            config.ANDROID_SDK_ROOT / "platforms" / f"android-{config.API_LEVEL}" / "android.jar",
        f"system-images;android-{config.API_LEVEL};google_apis;{ABI}":
            config.ANDROID_SDK_ROOT / "system-images" / f"android-{config.API_LEVEL}" / "google_apis" / ABI,
    }
    path = mapping.get(pkg)
    return path is not None and path.exists()


def _install_packages() -> None:
    packages = [
        "platform-tools",
        "emulator",
        f"platforms;android-{config.API_LEVEL}",
        f"system-images;android-{config.API_LEVEL};google_apis;{ABI}",
    ]
    to_install = [pkg for pkg in packages if not _package_installed(pkg)]
    if not to_install:
        print("[MDT] All SDK packages already installed ✓")
        return

    for pkg in to_install:
        print(f"[MDT] Installing {pkg} …")
        result = _run_sdkmanager("--install", pkg, input_text="y\n" * 5)
        if result.returncode != 0:
            print(f"[MDT] ✗  Failed to install {pkg}:")
            print(result.stderr[-600:])
            sys.exit(1)
        if not _package_installed(pkg):
            print(f"[MDT] ✗  {pkg} installation did not produce the expected files.")
            if result.stdout:
                print(result.stdout[-600:])
            if result.stderr:
                print(result.stderr[-600:])
            sys.exit(1)
        print(f"[MDT]   {pkg} ✓")


def check_acceleration() -> None:
    exe = config.ANDROID_SDK_ROOT / "emulator" / "emulator.exe"
    try:
        r = subprocess.run(
            [str(exe), "-accel-check"],
            env=_sdk_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        ok = r.returncode == 0 and "usable" in (r.stdout + r.stderr).lower()
    except Exception:
        ok = False
    if not ok:
        print("[MDT] ⚠  HARDWARE ACCELERATION NOT AVAILABLE — emulators will run in slow software mode.")
        print("[MDT]    Enable it in an ADMIN PowerShell, then reboot:")
        print("[MDT]    Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All")
    else:
        print("[MDT] Hardware acceleration OK (WHPX) ✓")


def bootstrap() -> None:
    ensure_system_prerequisites()

    if not SDKMANAGER.exists():
        _download_cmdline_tools()
    else:
        print("[MDT] cmdline-tools present ✓")

    _accept_licenses()
    _install_packages()

    adb_path = config.ANDROID_SDK_ROOT / "platform-tools" / "adb.exe"
    emu_path = config.ANDROID_SDK_ROOT / "emulator" / "emulator.exe"
    if not adb_path.exists():
        print(f"[MDT] ✗  adb not found at {adb_path}. Check SDK installation.")
        sys.exit(1)
    if not emu_path.exists():
        print(f"[MDT] ✗  emulator not found at {emu_path}. Check SDK installation.")
        sys.exit(1)

    print("[MDT] SDK bootstrap complete ✓\n")
