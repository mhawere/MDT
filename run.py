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


def setup_environment() -> None:
    """Resolve Android SDK path and export env vars before app imports."""
    # Minimal config bootstrap — full config import happens after env is set.
    sys.path.insert(0, str(PROJECT_ROOT))
    import config
    from app.sdk_config import (
        apply_sdk_root,
        is_project_local_sdk,
        refresh_tool_paths,
        resolve_sdk_root,
        validate_sdk,
    )

    root = resolve_sdk_root()
    apply_sdk_root(root)
    refresh_tool_paths()
    config.ANDROID_AVD_HOME.mkdir(parents=True, exist_ok=True)


setup_environment()

# Now safe to import config and app modules
import config
from app.sdk import bootstrap
from app.sdk_config import is_project_local_sdk, validate_sdk


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

    root = config.ANDROID_SDK_ROOT
    validation = validate_sdk(root)

    # 1. Bootstrap project-local SDK (idempotent — fast on subsequent runs)
    if is_project_local_sdk(root):
        bootstrap()
    elif not validation["valid"]:
        print(f"\n[MDT] ⚠  Android SDK at {root} is incomplete.")
        print("[MDT]    Missing:", ", ".join(validation.get("missing") or []))
        print("[MDT]    Open Settings in the UI to set or auto-detect your SDK path.\n")
    else:
        print(f"[MDT] Using Android SDK at {root} ✓")

    from app.sdk import check_acceleration
    if validation["valid"] or is_project_local_sdk(root):
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
