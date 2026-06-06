"""
run.py — Entrypoint: set env → bootstrap SDK → launch uvicorn → open browser.
"""
from __future__ import annotations

import os
import webbrowser
import subprocess
import sys
from pathlib import Path

# ── Resolve project root and set env BEFORE importing app modules ─────────────
PROJECT_ROOT = Path(__file__).parent.resolve()

def _prepend_path(*dirs: Path) -> None:
    existing = os.environ.get("PATH", "")
    new_dirs = os.pathsep.join(str(d) for d in dirs if d.exists())
    if new_dirs:
        os.environ["PATH"] = new_dirs + os.pathsep + existing

def setup_environment() -> None:
    sdk_root = PROJECT_ROOT / ".android-sdk"
    avd_home = sdk_root / "avd"

    os.environ["ANDROID_SDK_ROOT"] = str(sdk_root)
    os.environ["ANDROID_HOME"]     = str(sdk_root)
    os.environ["ANDROID_AVD_HOME"] = str(avd_home)

    _prepend_path(
        sdk_root / "platform-tools",
        sdk_root / "emulator",
        sdk_root / "cmdline-tools" / "latest" / "bin",
    )
    # Ensure avd home exists so avdmanager doesn't complain
    avd_home.mkdir(parents=True, exist_ok=True)

setup_environment()

# Now safe to import config and app modules
import config
from app.sdk import bootstrap


def _ensure_runtime_dependency(module_name: str) -> None:
    """Install project requirements once if a required runtime module is missing."""
    try:
        __import__(module_name)
        return
    except ModuleNotFoundError:
        pass

    requirements = PROJECT_ROOT / "requirements.txt"
    print(f"[MDT] Missing Python module '{module_name}'. Installing dependencies …")
    try:
        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(requirements),
        ])
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Failed to install Python dependencies. "
            f"Run: {sys.executable} -m pip install -r {requirements}"
        ) from exc

    __import__(module_name)


def open_browser(url: str) -> None:
    webbrowser.open(url)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  Multi-Device Tester (MDT)")
    print("=" * 60)

    # 1. Bootstrap SDK (idempotent — fast on subsequent runs)
    bootstrap()
    from app.sdk import check_acceleration
    check_acceleration()

    # 2. Start server + open browser
    url = f"http://localhost:{config.SERVER_PORT}"
    print(f"[MDT] Starting server at {url}")

    import threading
    import time

    def _open_after_delay():
        time.sleep(2)
        open_browser(url)

    threading.Thread(target=_open_after_delay, daemon=True).start()

    # 3. Run uvicorn
    _ensure_runtime_dependency("uvicorn")
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
