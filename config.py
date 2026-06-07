"""
config.py — single source of truth for all MDT paths, ports, and tunable constants.
All path derivations happen in run.py after PROJECT_ROOT is known.
"""
import os
from pathlib import Path

# ── Emulator / SDK ────────────────────────────────────────────────────────────
API_LEVEL            = 34
DEVICE_PROFILE       = "pixel_5"
MAX_DEVICES          = 3
EMULATOR_MEMORY_MB   = 4096
EMULATOR_BOOT_TIMEOUT= 180   # seconds
EMULATOR_BOOT_CONCURRENCY = 1
EMULATOR_GPU_MODE    = "host"
INPUT_SWIPE_MS       = 140

# ── Streaming ─────────────────────────────────────────────────────────────────
SCREENRECORD_SIZE        = "720x1560"   # WxH; must match device aspect; None for native
SCREENRECORD_BITRATE     = 8_000_000    # bits per second
SCREENRECORD_TIME_LIMIT  = 180          # screenrecord hard max; we respawn on expiry

# ── Server ────────────────────────────────────────────────────────────────────
SERVER_HOST          = "127.0.0.1"
SERVER_PORT          = 8000

# ── Directories (relative names — resolved to abs paths by run.py) ────────────
APK_DIR              = "apk_input"
LOG_DIR              = "logs"
SDK_DIR              = ".android-sdk"
VENV_DIR             = ".venv"

# ── Derived absolute paths (populated by run.py before import of app.*) ───────
PROJECT_ROOT: Path   = Path(__file__).parent.resolve()
ANDROID_SDK_ROOT: Path = PROJECT_ROOT / SDK_DIR
ANDROID_AVD_HOME: Path = ANDROID_SDK_ROOT / "avd"
APK_INPUT_DIR: Path  = PROJECT_ROOT / APK_DIR
LOG_OUTPUT_DIR: Path = PROJECT_ROOT / LOG_DIR

# ── Emulator port scheme ───────────────────────────────────────────────────────
# device i → console 5554+i*2, adb 5555+i*2
def emulator_ports(index: int) -> tuple[int, int]:
    """Return (console_port, adb_port) for device at index."""
    return 5554 + index * 2, 5555 + index * 2

def emulator_serial(index: int) -> str:
    console, _ = emulator_ports(index)
    return f"emulator-{console}"
