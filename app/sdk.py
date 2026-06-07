"""
sdk.py — Bootstrap the project-local Android SDK (cross-platform).

Downloads cmdline-tools if missing, accepts licenses, installs required packages,
and checks hardware acceleration at startup.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import config
from app.sdk_config import (
    PLATFORM,
    SdkNotReadyError,
    SDK_NOT_CONFIGURED_MSG,
    find_cmdline_bin,
    is_project_local_sdk,
)

CMDLINE_TOOLS_VERSION = "11076708"
ABI = "x86_64"
SUPPORTED_PLATFORMS = frozenset({"win", "linux", "mac"})


def _sdk_script_name(tool: str) -> str:
    """sdkmanager / avdmanager — .bat on Windows only."""
    return f"{tool}.bat" if PLATFORM == "win" else tool


def _bin_name(tool: str) -> str:
    """adb / emulator / aapt2 — .exe on Windows only."""
    return f"{tool}.exe" if PLATFORM == "win" else tool


def sdkmanager_path() -> Path:
    found = find_cmdline_bin(config.ANDROID_SDK_ROOT, "sdkmanager")
    if found:
        return found
    return (
        config.ANDROID_SDK_ROOT
        / "cmdline-tools"
        / "latest"
        / "bin"
        / _sdk_script_name("sdkmanager")
    )


def avdmanager_path() -> Path:
    found = find_cmdline_bin(config.ANDROID_SDK_ROOT, "avdmanager")
    if found:
        return found
    return (
        config.ANDROID_SDK_ROOT
        / "cmdline-tools"
        / "latest"
        / "bin"
        / _sdk_script_name("avdmanager")
    )


def adb_path() -> Path:
    return config.ANDROID_SDK_ROOT / "platform-tools" / _bin_name("adb")


def emulator_bin_path() -> Path:
    return config.ANDROID_SDK_ROOT / "emulator" / _bin_name("emulator")


def aapt2_path_in(ver_dir: Path) -> Path:
    return ver_dir / _bin_name("aapt2")


# Resolved once at import for backward compatibility with existing imports.
SDKMANAGER = sdkmanager_path()
AVDMANAGER = avdmanager_path()


def ensure_platform_supported() -> None:
    if PLATFORM in SUPPORTED_PLATFORMS:
        return
    name = platform.system() or sys.platform
    print(f"\n[MDT] ✗  Unsupported platform: {name} ({sys.platform})")
    print("[MDT]    MDT supports Windows, Linux, and macOS.")
    sys.exit(1)


def ensure_system_prerequisites() -> None:
    ensure_platform_supported()
    if not shutil.which("java"):
        print("\n[MDT] ⚠  Missing system prerequisite: java")
        print("[MDT]    Install a JDK (17+) and ensure java is on PATH, then re-run MDT.")
        if PLATFORM == "win":
            print("[MDT]    Example (PowerShell): winget install EclipseAdoptium.Temurin.17.JDK")
        elif PLATFORM == "linux":
            print("[MDT]    Example: sudo apt install openjdk-17-jdk")
        elif PLATFORM == "mac":
            print("[MDT]    Example: brew install openjdk@17")
        print()
        sys.exit(1)


def _cmdline_tools_archive() -> str:
    archives = {
        "win": f"commandlinetools-win-{CMDLINE_TOOLS_VERSION}_latest.zip",
        "linux": f"commandlinetools-linux-{CMDLINE_TOOLS_VERSION}_latest.zip",
        "mac": f"commandlinetools-mac-{CMDLINE_TOOLS_VERSION}_latest.zip",
    }
    return archives[PLATFORM]


def _download_cmdline_tools() -> None:
    archive = _cmdline_tools_archive()
    url = f"https://dl.google.com/android/repository/{archive}"
    zip_path = config.ANDROID_SDK_ROOT / "cmdline-tools.zip"
    config.ANDROID_SDK_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"[MDT] Downloading Android cmdline-tools ({PLATFORM}) from:\n      {url}")
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


def _build_tools_installed() -> bool:
    build_tools = config.ANDROID_SDK_ROOT / "build-tools"
    if not build_tools.exists():
        return False
    for ver_dir in build_tools.iterdir():
        if aapt2_path_in(ver_dir).exists():
            return True
    return False


def _package_installed(pkg: str) -> bool:
    mapping = {
        "platform-tools": adb_path(),
        "emulator": emulator_bin_path(),
        "build-tools": None,
        f"platforms;android-{config.API_LEVEL}":
            config.ANDROID_SDK_ROOT / "platforms" / f"android-{config.API_LEVEL}" / "android.jar",
        f"system-images;android-{config.API_LEVEL};google_apis;{ABI}":
            config.ANDROID_SDK_ROOT / "system-images" / f"android-{config.API_LEVEL}" / "google_apis" / ABI,
    }
    if pkg == "build-tools":
        return _build_tools_installed()
    path = mapping.get(pkg)
    return path is not None and path.exists()


def _install_packages() -> None:
    packages = [
        "platform-tools",
        "emulator",
        "build-tools",
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


def _accel_hint() -> None:
    if PLATFORM == "win":
        print("[MDT]    Enable it in an ADMIN PowerShell, then reboot:")
        print("[MDT]    Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All")
    elif PLATFORM == "linux":
        print("[MDT]    On Linux, ensure KVM is enabled and your user is in the kvm group:")
        print("[MDT]    sudo usermod -aG kvm $USER  (then log out and back in)")
    elif PLATFORM == "mac":
        print("[MDT]    On macOS, ensure no other hypervisor is blocking the Android Emulator.")


def check_acceleration() -> None:
    exe = emulator_bin_path()
    if not exe.exists():
        print(f"[MDT] ⚠  Emulator binary not found at {exe}; skipping acceleration check.")
        return
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
        _accel_hint()
    else:
        label = { "win": "WHPX", "linux": "KVM", "mac": "Hypervisor" }.get(PLATFORM, "acceleration")
        print(f"[MDT] Hardware acceleration OK ({label}) ✓")


def bootstrap(*, exit_on_failure: bool = True) -> None:
    ensure_system_prerequisites()

    if not SDKMANAGER.exists():
        _download_cmdline_tools()
        refresh_tool_paths()
    else:
        print("[MDT] cmdline-tools present ✓")

    _accept_licenses()
    _install_packages()
    refresh_tool_paths()

    adb = adb_path()
    emu = emulator_bin_path()
    if not adb.exists():
        msg = f"[MDT] ✗  adb not found at {adb}. Check SDK installation."
        if exit_on_failure:
            print(msg)
            sys.exit(1)
        raise SdkNotReadyError(msg)
    if not emu.exists():
        msg = f"[MDT] ✗  emulator not found at {emu}. Check SDK installation."
        if exit_on_failure:
            print(msg)
            sys.exit(1)
        raise SdkNotReadyError(msg)

    print("[MDT] SDK bootstrap complete ✓\n")


def refresh_tool_paths() -> None:
    """Update module-level SDKMANAGER / AVDMANAGER after SDK root changes."""
    global SDKMANAGER, AVDMANAGER
    SDKMANAGER = sdkmanager_path()
    AVDMANAGER = avdmanager_path()


def ensure_sdk_ready() -> None:
    """
    Validate SDK before device orchestration.
    Auto-bootstraps project-local .android-sdk when cmdline-tools are missing.
    """
    from app.sdk_config import apply_sdk_root, refresh_tool_paths as _refresh_cfg
    from app.sdk_config import resolve_sdk_root, validate_sdk

    root = resolve_sdk_root()
    apply_sdk_root(root)
    _refresh_cfg()

    validation = validate_sdk(root)
    if validation["ready"]:
        refresh_tool_paths()
        return

    if is_project_local_sdk(root):
        bootstrap(exit_on_failure=False)
        validation = validate_sdk(root)
        if validation["valid"]:
            refresh_tool_paths()
            return

    missing = ", ".join(validation.get("missing") or ["sdk"])
    if not validation["valid"]:
        raise SdkNotReadyError(
            f"{SDK_NOT_CONFIGURED_MSG} Missing: {missing}."
        )
    refresh_tool_paths()
